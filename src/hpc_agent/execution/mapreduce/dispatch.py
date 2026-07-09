#!/usr/bin/env python3
"""Standalone task dispatcher deployed to the HPC cluster.

This script is scp'd to ``$REMOTE_PATH/.hpc/_hpc_dispatch.py`` by
``deploy_runtime`` and executed by the SGE/SLURM array-job template.

Per-task identity comes from ``HPC_TASK_ID``; per-run identity from
``HPC_RUN_ID``. The dispatcher:

1. Imports the user's ``.hpc/tasks.py`` (sibling of this file) and calls
   ``resolve(task_id)`` to get the per-task kwargs.
2. Reads the per-run sidecar at ``.hpc/runs/<HPC_RUN_ID>.json`` for the
   executor command and result-directory template.
3. Formats the result directory using kwargs + ``task_id``.
4. Sets the kwargs as env vars (both raw uppercase and ``HPC_KW_*``).
5. Runs the executor in a shell with WIP / atomic-promote semantics so
   crashed mid-runs never pollute the final result directory.

Stays zero-dependency — only Python stdlib, no imports from the
``hpc_agent`` package.
"""

import contextlib
import datetime
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

__all__ = ["main"]

# Number of trailing bytes of the executor's stderr we retain in memory
# and write into the failure directory on a non-zero exit. Bounded so a
# pathological executor that floods stderr can't blow up the dispatcher's
# RSS; the tail is what a campus user needs to diagnose the crash.
_STDERR_TAIL_BYTES = 64 * 1024

# Default grace window in seconds. The scheduler typically sends SIGTERM
# 30-60s before SIGKILL on preemption; we forward SIGINT to the executor
# subprocess and wait up to this many seconds for it to exit before
# tearing down. Override via ``HPC_PREEMPT_GRACE_SEC``.
_DEFAULT_PREEMPT_GRACE_SEC = 25

# Exit code used when the dispatcher itself is preempted by the
# scheduler. 130 = 128 + 2 (SIGINT). hpc-agent treats the trapped
# SIGTERM as if the executor received SIGINT, so the campus user's
# agent harness sees the canonical "job interrupted" code regardless
# of which signal the scheduler actually sent — preempted runs survive
# as a clean, recognizable diagnostic instead of a noisy crash.
_EXIT_PREEMPTED = 130

# Exit code used when no valid per-task runner could be resolved — the
# sidecar's executor is empty or would re-invoke the dispatcher itself
# (the #162 self-recursion footgun). Distinct from the generic
# user/config exit (1) and schema-mismatch exit (2) so the cluster-side
# retry wrapper can treat it as terminal (a deterministic scaffold error
# that retrying cannot fix) rather than a transient failure to back off.
_EXIT_NO_RUNNER = 3

# Exit code used when the executor exits 0 but writes NOTHING to its WIP
# result dir — it produced no result to promote. Distinct from the generic
# failure (1), schema (2), and no-runner (3) codes so the cluster-side retry
# wrapper treats it as TERMINAL: an executor whose __main__ only prints,
# bypassing the framework's result-writer, is a deterministic scaffold error
# that retrying cannot fix. Promoting the empty dir as a success was the
# proving-run-5 finding-16 FALSE GREEN — every task read "complete", the
# 1-task canary passed, the full array ran, and only the harvest discovered
# there was nothing to aggregate. Marking a no-output run FAILED lets the
# reporter count it failed and the canary catch it on one task before paying
# for N. Kept in lock-step with HPC_DISPATCH_EXIT_NO_OUTPUT in hpc_preamble.sh.
_EXIT_NO_OUTPUT = 4

# Sidecar schema versions this dispatcher accepts. Kept in sync with
# ``SIDECAR_SCHEMA_VERSION`` in ``hpc_agent/state/runs.py``. Hardcoded
# here because this module must stay stdlib-only.
#
# v2 added optional fields (wave_map, aggregate_defaults, ...). The
# dispatcher reads only the v1-shape fields (sidecar_schema_version,
# executor, result_dir_template), so accepting v2 here is safe; the
# extra fields are simply ignored cluster-side.
SUPPORTED_SCHEMA_VERSIONS = (1, 2)


def _load_tasks_module(tasks_py_path):
    spec = importlib.util.spec_from_file_location("hpc_user_tasks", tasks_py_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load tasks.py from {tasks_py_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _utcnow_iso():
    """Return current UTC timestamp in ISO 8601 format with ``Z`` suffix.

    Stdlib-only; mirrors the ``utcnow_iso`` helper used elsewhere in the
    framework but inlined here because dispatch.py cannot import from
    ``hpc_agent.*`` (cluster-side stdlib-only constraint).
    """
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _atomic_write_json(path, data):
    """Atomically write *data* as JSON to *path*.

    Writes to a sibling tempfile in the same directory, then
    ``os.replace``\\ s into place. Same-filesystem rename guarantees
    readers never see a half-written sidecar. Stdlib-only.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def _mark_preempted_in_sidecar(sidecar_path, task_id, when_iso, *, grace_sec):
    """Write ``preempt: {at, grace_sec}`` to the per-task sidecar entry.

    Marks the run as bumped (preempted by higher-priority work), not
    failed. The agent harness reads this field to distinguish a clean
    resubmit from a real failure. Best-effort: a write error here must
    not prevent the dispatcher from exiting, since the SIGKILL window
    is short.

    Namespaced under ``preempt`` (not flat ``preempted_at``) to avoid
    field-name collisions with future preemption-related metadata and
    to keep the sidecar reading clean for the campus user inspecting
    a bumped run by hand.
    """
    try:
        sidecar = json.loads(Path(sidecar_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return
    tasks = sidecar.setdefault("tasks", {})
    if not isinstance(tasks, dict):
        return
    entry = tasks.setdefault(str(task_id), {})
    if not isinstance(entry, dict):
        return
    entry["preempt"] = {"at": when_iso, "grace_sec": int(grace_sec)}
    # Sidecar lives on shared NFS; a transient write failure here is
    # survivable — the agent harness will fall back to exit-code 130
    # detection. Don't let it block the preemption-window teardown.
    with contextlib.suppress(OSError):
        _atomic_write_json(sidecar_path, sidecar)


def _install_preemption_handler(*, sidecar_path, task_id, child_holder, grace_sec):
    """Install a SIGTERM handler that marks the run preempted and tears down.

    The handler:
      1. Ignores subsequent SIGTERMs for the rest of its lifetime
         (re-entrancy guard — a flurry of SIGTERMs in the cluster's
         preemption window must not recursively re-enter the handler
         and call ``sys.exit`` while the outer call is still unwinding).
      2. Logs to stderr (matches the existing dispatch.py prose style).
      3. Writes ``preempt: {at: <utcnow_iso>, grace_sec: <int>}`` to the
         per-task entry of the run sidecar so the agent harness can tell
         "bumped" from "real failure".
      4. Forwards SIGINT to the executor subprocess so its except blocks
         run during the cluster's preemption window. If the SIGTERM lands
         in the race window between ``Popen()`` returning and the main
         flow assigning into ``child_holder[0]``, the dispatcher has no
         handle to the orphaned child — the executor is in its own
         process group (via ``preexec_fn=os.setpgrp``) and was never
         reachable via the dispatcher's pgid. We log the orphan and exit;
         the cgroup teardown will eventually reap it. Documented limitation,
         not a fixable race in pure Python.
      5. Waits up to *grace_sec* for the executor to exit cleanly, then
         escalates ``terminate() → wait(2) → kill()`` to guarantee
         teardown. A zombie executor outliving the dispatcher would keep
         writing to a half-rotated log and surprise the next user of
         the same node.
      6. Exits 130 — the POSIX-standard preempted exit code.

    *child_holder* is a single-element list holding the current
    ``subprocess.Popen`` (or ``None`` if no child is live yet); using a
    list lets us mutate the slot from the main flow without rebinding
    the closure.
    """

    def _handler(signum, frame):
        # A-H3: re-entrancy guard. Ignore further SIGTERMs for the rest
        # of this process's life. SIG_IGN is set first because writing
        # the sidecar and rsync-style waits below take measurable
        # wall-clock; a second SIGTERM mid-handler would otherwise
        # recurse into another sys.exit while the outer call is still
        # unwinding — a footgun the campus user can't debug from a
        # cluster log.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

        print(
            "[hpc-agent] SIGTERM received; cluster preemption imminent",
            file=sys.stderr,
        )
        _mark_preempted_in_sidecar(sidecar_path, task_id, _utcnow_iso(), grace_sec=grace_sec)

        child = child_holder[0] if child_holder else None
        if child is not None and child.poll() is None:
            # Signal the child's whole process group, not just child.pid.
            # The executor runs under ``shell=True`` and was placed in
            # its own process group (preexec_fn=os.setpgrp), so it is the
            # group leader (pgid == child.pid). child.send_signal /
            # .terminate / .kill hit only the shell pid — if the shell
            # forks the real workload, the signal never reaches it.
            with contextlib.suppress(OSError):
                os.killpg(child.pid, signal.SIGINT)
            deadline = time.monotonic() + max(0, int(grace_sec))
            while time.monotonic() < deadline and child.poll() is None:
                time.sleep(0.5)
            if child.poll() is None:
                # A-H2: escalate. The executor ignored or blocked the
                # SIGINT we forwarded; terminate-then-kill so we don't
                # leave an orphan that keeps writing to the next user's
                # half-rotated log file after the cgroup eventually
                # collects it.
                with contextlib.suppress(OSError):
                    os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(OSError):
                        os.killpg(child.pid, signal.SIGKILL)
                    with contextlib.suppress(Exception):
                        child.wait(timeout=2)
        else:
            # A-H1: race window. SIGTERM landed before the main flow
            # could populate child_holder[0]. The child (if Popen
            # already returned but the assignment didn't land) is in its
            # OWN process group (preexec_fn=os.setpgrp made it the
            # leader), so we can't reach it via the dispatcher's pgid.
            # Log the orphan honestly; cgroup teardown is the eventual
            # cleanup. This is a documented limitation, not a fixable
            # race in pure Python.
            print(
                "[hpc-agent] SIGTERM in race window before child handle was "
                "captured; child (if any) was placed in its own process group "
                "and cannot be signaled. Cgroup teardown will reap it.",
                file=sys.stderr,
            )

        sys.exit(_EXIT_PREEMPTED)

    signal.signal(signal.SIGTERM, _handler)


def _executor_reinvokes_dispatcher(executor, *, dispatcher_path):
    """True when *executor* would re-invoke this dispatcher itself.

    Guards against the self-recursion footgun that burned 8 nodes live
    (#162): when submit-flow ships a run sidecar whose ``executor`` was
    synthesized from the job script's command (``python3
    .hpc/_hpc_dispatch.py`` — the dispatcher) instead of a real per-task
    command, the dispatcher resolves that and re-enters itself, which
    resolves the same command, and so on — an instant self-recursion the
    array template then retried in a tight loop.

    The check is intentionally conservative: it matches on the
    dispatcher's own basename (``_hpc_dispatch.py``, the deployed name)
    anywhere in the resolved command string. A real per-task command runs
    the user's script (``python train.py``, ``python3 -m <module>``); none
    reference the dispatcher's filename, so this never false-positives on a
    real runner. Substring (not exact) match because the command is a shell
    string that may carry flags / redirections around the script path.
    """
    if not executor:
        return False
    self_name = Path(dispatcher_path).name  # e.g. _hpc_dispatch.py
    candidates = {self_name}
    # ``dispatch.py`` is the in-repo source name; the deployed copy is
    # ``_hpc_dispatch.py``. Match both so a spec that points at either
    # spelling is caught regardless of how it was assembled.
    candidates.add("_hpc_dispatch.py")
    candidates.add("dispatch.py")
    return any(name in executor for name in candidates)


# Launcher templates for a multi-rank job (#293), keyed by HPC_MPI_LAUNCHER.
# ``{n}`` is the rank count from HPC_MPI_RANKS. Kept in lock-step with the
# ``case`` arms documented on the mpi template (infra/backends/_scripts.py).
_MPI_LAUNCHERS = {
    "srun": "srun --ntasks={n}",
    "mpirun": "mpirun -np {n}",
    "aprun": "aprun -n {n}",
}


def _mpi_launch_prefix(env):
    """Return the launcher prefix for a multi-rank job, or ``""`` if not MPI.

    Reads ``HPC_MPI_RANKS`` / ``HPC_MPI_LAUNCHER`` (stamped into the job env by
    build-submit-spec from the spec's ``mpi`` block). Prefixing the per-task
    command — rather than wrapping the whole template in ``srun`` — keeps the
    dispatcher's bookkeeping (sidecar, WIP dir, failure capture, SIGTERM
    forwarding) a single process while only the compute fans out to N ranks.
    The reducer still sees one ``metrics.json`` (rank 0 writes it).
    """
    ranks = (env.get("HPC_MPI_RANKS") or "").strip()
    if not ranks:
        return ""
    try:
        n = int(ranks)
    except ValueError:
        print(f"[dispatch] WARN: ignoring non-integer HPC_MPI_RANKS={ranks!r}", file=sys.stderr)
        return ""
    if n <= 1:
        return ""  # a single rank needs no launcher
    launcher = (env.get("HPC_MPI_LAUNCHER") or "srun").strip()
    template = _MPI_LAUNCHERS.get(launcher)
    if template is None:
        print(
            f"[dispatch] WARN: unknown HPC_MPI_LAUNCHER={launcher!r}; "
            f"expected one of {sorted(_MPI_LAUNCHERS)}; running un-launched",
            file=sys.stderr,
        )
        return ""
    return template.format(n=n)


# Match ``$HPC_KW_FOO`` and ``${HPC_KW_FOO}`` references in a shell command.
# Restricted to the HPC_KW_ namespace on purpose: the bare-uppercase legacy
# exports collide with real environment vars ($HOME, $PATH, ...), so a bare
# ``$SAMPLES`` reference can't be reliably told apart from a genuine env var the
# user expects to inherit. The HPC_KW_ prefix is framework-owned and unambiguous.
_HPC_KW_REF_RE = re.compile(r"\$\{?(HPC_KW_[A-Za-z0-9_]+)\}?")


def _warn_unset_kwarg_refs(executor, env):
    """Warn (stderr) about ``$HPC_KW_*`` refs in *executor* not set in *env*.

    Defense-in-depth for #195: the static ``validate-executor-signatures`` gate
    refuses a spec whose tasks.resolve() leaves a required signature param
    uncovered, but it can only see the executor *function's* signature. An
    executor command template that references ``$HPC_KW_X`` directly — for a
    param outside the introspectable signature — slips past that gate. Here, at
    the dispatch site, we know the exact command string AND the exact exported
    env, so we can diff them: any ``$HPC_KW_*`` the command references but the
    kwargs never produced expands to empty and silently corrupts the run (the
    argparse "expected one argument" / empty-flag failure mode from #195).

    Warn rather than abort: the canary (one task) surfaces the empty-expansion
    failure loudly enough, and a hard abort here could false-positive on a
    template that legitimately guards an optional ``${HPC_KW_X:-default}`` (the
    ``:-`` default form still matches the bare name but is in fact safe). The
    warning rides the cluster job log so a post-mortem names the exact var.
    """
    referenced = set(_HPC_KW_REF_RE.findall(executor or ""))
    missing = sorted(name for name in referenced if name not in env)
    for name in missing:
        kwarg = name[len("HPC_KW_") :].lower()
        print(
            f"[dispatch] WARN: executor command references ${name} but no "
            f"{kwarg!r} kwarg was exported — it will expand to empty and the "
            f"command may fail (e.g. argparse 'expected one argument'). Cover "
            f"{kwarg!r} as a sweep axis or entry_point.fixed_params (#195).",
            file=sys.stderr,
        )
    return missing


def _pump_stderr(pipe, *, tail_buf, tail_lock, max_bytes):
    """Relay the executor's stderr to ours while retaining a bounded tail.

    Reads *pipe* line-by-line, writes each chunk straight through to the
    dispatcher's own stderr (so the cluster's per-task job log keeps the
    executor's diagnostics exactly as before this capture was added), and
    appends to *tail_buf* under *tail_lock*, trimming to the last
    *max_bytes*. The trimmed tail is what gets persisted into the failure
    directory on a non-zero exit (#161 — failures used to leave EMPTY
    ``_wip_*_failed_*`` dirs with no clue why).

    Runs on a daemon thread so a wedged read can never block the
    dispatcher's exit. Best-effort throughout: any I/O error while
    relaying must not crash the dispatcher (the task's own exit code is
    what matters), so the loop swallows OSErrors and returns.
    """
    try:
        for raw in iter(pipe.readline, b""):
            with contextlib.suppress(OSError, ValueError):
                sys.stderr.buffer.write(raw)
                sys.stderr.buffer.flush()
            with tail_lock:
                tail_buf.append(raw)
                # Trim from the front so memory stays bounded under a
                # chatty executor. Joining only to measure would be O(n^2)
                # over many lines; instead drop whole leading chunks until
                # the retained suffix is under the cap.
                total = sum(len(c) for c in tail_buf)
                while total > max_bytes and len(tail_buf) > 1:
                    total -= len(tail_buf.pop(0))
    except (OSError, ValueError):
        # Pipe closed / already-closed during teardown — nothing to relay.
        return


def _write_failure_capture(wip_dir, *, task_id, returncode, executor, stderr_tail):
    """Record why a dispatch failed into the WIP dir for forensics.

    Writes ``_hpc_dispatch_error.log`` next to the preserved partial
    output so the empty-failure-dir mystery (#161) becomes diagnosable:
    the campus user (and the agent's failure-classifier) can read the
    exit code, the exact per-task command, and the tail of the executor's
    stderr without spelunking the scheduler's array-wide job log.

    Best-effort: a write failure here must not change the dispatcher's
    own exit code (the task already failed on its own merits).
    """
    try:
        os.makedirs(wip_dir, exist_ok=True)
        log_path = os.path.join(wip_dir, "_hpc_dispatch_error.log")
        header = (
            f"task_id={task_id}\n"
            f"exit_code={returncode}\n"
            f"executor={executor}\n"
            f"when={_utcnow_iso()}\n"
            "--- executor stderr (tail) ---\n"
        ).encode()
        with open(log_path, "wb") as fh:
            fh.write(header)
            fh.write(stderr_tail)
            if stderr_tail and not stderr_tail.endswith(b"\n"):
                fh.write(b"\n")
    except OSError as exc:
        print(
            f"[dispatch] WARN: could not write failure capture in {wip_dir}: {exc}",
            file=sys.stderr,
        )


# #294: step-indexed checkpoint filename shapes, mirrored from
# ``hpc_agent.experiment_kit.checkpoint`` (.pkl) and
# ``...solver_adapters.petsc`` (.petscbin) — this module ships to the cluster
# as a standalone file (deploy_runtime) and cannot import the package, so the
# patterns are duplicated here by design. Order matters only on an
# equal-iteration tie: earlier pattern (pickle) wins, preserving the
# pre-petsc behavior for the runs that only ever write pickles.
_CHECKPOINT_RES = (
    re.compile(r"^checkpoint-(\d+)\.pkl$"),
    re.compile(r"^checkpoint-(\d+)\.petscbin$"),
)


def _latest_checkpoint(checkpoint_dir):
    """Absolute path to the highest-iteration non-empty checkpoint, or "" if none.

    Stdlib-only twin of ``experiment_kit.checkpoint.latest_checkpoint`` —
    widened to every step-indexed format (#294: pickle; solver-adapter PETSc
    binary dumps) so a resumed petsc4py executor also gets a concrete
    ``HPC_RESUME_FROM``. On ``resubmit --from-checkpoint`` the dispatcher uses
    this to hand the executor a resume point. Skips 0-byte files (a crash
    mid-write, pre-atomic) and never raises — a missing/unreadable dir just
    means "no checkpoint, start fresh". Wrapper-path PETSc dumps
    (``petsc-solution.bin``) are deliberately NOT scanned: the instrumented
    wrapper rotates and consumes those itself (``promote_restart``) and never
    reads ``HPC_RESUME_FROM``.
    """
    best = ""
    # (iteration, pattern_priority) — higher wins; priority breaks an
    # equal-iteration tie deterministically (listdir order is arbitrary).
    best_key = (-1, -1)
    try:
        names = os.listdir(checkpoint_dir)
    except OSError:
        return ""
    for name in names:
        m = None
        priority = -1
        for idx, pattern in enumerate(_CHECKPOINT_RES):
            m = pattern.match(name)
            if m:
                priority = len(_CHECKPOINT_RES) - idx
                break
        if not m:
            continue
        path = os.path.join(checkpoint_dir, name)
        try:
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                key = (int(m.group(1)), priority)
                if key > best_key:
                    best_key, best = key, path
        except OSError:
            continue
    return best


# Prefix every injected service var gets, so an address can never silently
# clobber an executor's $HOME/$PATH (the bare-uppercase footgun the HPC_KW_*
# namespacing already guards against on the kwargs side). Stdlib-only twin of
# ``hpc_agent.ops.recover.service.SERVICE_ENV_NAMESPACE`` — this file ships
# standalone (deploy_runtime never ships the package), so the contract is
# duplicated here by design; behavior parity is pinned by
# tests/execution/mapreduce/test_dispatch.py::TestServiceEnvPassthrough.
_SERVICE_ENV_NAMESPACE = "HPC_SERVICE_"


def _inject_service_env(env, service_env):
    """Thread an externally-provisioned service address into a task env.

    Each ``service_env`` entry ships as ``HPC_SERVICE_<KEY>`` (namespaced,
    collision-free), mirroring the ``HPC_KW_*`` kwarg contract. Returns *env*
    (mutated in place). A ``None``/empty *service_env* is a clean no-op.

    Stdlib-only twin of ``hpc_agent.ops.recover.service.inject_service_env``,
    inlined because the dispatcher cannot import the package cluster-side
    (#231 Tier 1 passthrough; the ``from hpc_agent.ops...`` form died with
    ModuleNotFoundError on every array task of a service_env run).
    """
    if not service_env:
        return env
    for key, value in service_env.items():
        env[f"{_SERVICE_ENV_NAMESPACE}{key.upper()}"] = str(value)
    return env


def _task_is_included(task_id, env):
    """True when *task_id* is in the ``HPC_TASK_INCLUDE`` partial-reproduction set.

    The execution-restriction seam for a partial reproduction (determinism-
    fingerprint design center 5): ``reproduce-run`` keeps the FULL task shape
    (same trial_params / cmd_sha — a rebuilt smaller task list would move cmd_sha
    and orphan the fingerprint ledger) and instead threads the selected task
    indices through the job env as ``HPC_TASK_INCLUDE`` (a comma-separated
    include-list). A non-selected index exits 0 immediately (the idempotency
    skip's sibling seam), so its scheduler slot costs milliseconds.

    Absent / blank ``HPC_TASK_INCLUDE`` means NO restriction — every task runs
    (an ordinary full run). A malformed value that parses to an EMPTY set is
    treated as no-restriction too (never silently skip the whole array on a bad
    env var — the canary would surface the mis-parse, not a mass no-op).
    """
    raw = env.get("HPC_TASK_INCLUDE")
    if raw is None or not raw.strip():
        return True
    included = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            included.add(int(part))
        except ValueError:
            print(
                f"[dispatch] WARN: ignoring non-integer HPC_TASK_INCLUDE entry {part!r}",
                file=sys.stderr,
            )
    if not included:
        return True
    return task_id in included


def _format_result_dir(template, *, task_id, run_id, kwargs):
    """Render *template* using ``str.format`` with task/run identity + kwargs.

    Kwargs win over reserved keys on collision, matching the documented
    behaviour: the user's tasks.py controls the namespace.
    """
    ctx = {"task_id": task_id, "run_id": run_id, **kwargs}
    try:
        return template.format(**ctx)
    except KeyError as exc:
        raise KeyError(
            f"result_dir_template references unknown key {exc.args[0]!r}; available: {sorted(ctx)}"
        ) from None


def main() -> None:
    here = Path(__file__).resolve().parent  # cluster-side .hpc/

    # --- Resolve run identity & sidecar (before tasks.py to enable frozen-manifest fast path) ---
    run_id = os.environ.get("HPC_RUN_ID")
    if not run_id:
        print("[dispatch] ERROR: HPC_RUN_ID env var not set", file=sys.stderr)
        sys.exit(1)

    sidecar_path = here / "runs" / f"{run_id}.json"
    if not sidecar_path.is_file():
        print(f"[dispatch] ERROR: run sidecar not found: {sidecar_path}", file=sys.stderr)
        sys.exit(1)
    try:
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"[dispatch] ERROR: failed to parse sidecar: {exc}", file=sys.stderr)
        sys.exit(1)

    schema_version = sidecar.get("sidecar_schema_version")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        print(
            f"[dispatch] ERROR: sidecar schema_version={schema_version}, "
            f"supported={list(SUPPORTED_SCHEMA_VERSIONS)}. Re-submit with current hpc-agent.",
            file=sys.stderr,
        )
        sys.exit(2)

    executor = sidecar.get("executor")
    result_dir_template = sidecar.get("result_dir_template")
    if not executor or not result_dir_template:
        print(
            "[dispatch] ERROR: sidecar missing executor or result_dir_template",
            file=sys.stderr,
        )
        sys.exit(1)

    # --- Fail loud on a self-referential per-task runner ---
    # The per-task command must NOT be the dispatcher itself. When a submit
    # ships a run sidecar whose ``executor`` was synthesized from the job
    # script's command (the dispatcher) instead of a real per-task command,
    # the resolved command points back at ``_hpc_dispatch.py`` — running it
    # re-enters this dispatcher in an infinite self-recursion that the array
    # template then retries in a tight loop (the live #162 incident: ~8,647
    # attempts in 12 min, 8 nodes burned). Abort with a clear, terminal
    # error and a non-zero exit BEFORE spawning anything, so the template's
    # retry guard records the task as failed instead of looping.
    #
    # Exit ``_EXIT_NO_RUNNER`` (3) is distinct from the generic user/config
    # exit (1) and the schema-mismatch exit (2) so the cluster-side retry
    # wrapper recognises "deterministic — do not bother retrying."
    if _executor_reinvokes_dispatcher(executor, dispatcher_path=__file__):
        print(
            f"[dispatch] ERROR: the run sidecar's per-task executor ({executor!r}) "
            "re-invokes the dispatcher itself — refusing to self-recurse. The sidecar "
            "must carry the real per-task command (e.g. `python train.py --seed "
            "$SEED`), not the job script's dispatcher command. submit-flow wrote a "
            "sidecar whose `executor` was never set to a per-task command (Step 6d / "
            "write_run_sidecar skipped). Re-submit with a real per-task executor.",
            file=sys.stderr,
        )
        sys.exit(_EXIT_NO_RUNNER)

    # --- Resolve task identity ---
    task_id_str = os.environ.get("HPC_TASK_ID") or os.environ.get("TASK_ID")
    if task_id_str is None:
        print("[dispatch] ERROR: HPC_TASK_ID env var not set", file=sys.stderr)
        sys.exit(1)
    try:
        task_id = int(task_id_str)
    except ValueError:
        print(f"[dispatch] ERROR: HPC_TASK_ID is not an integer: {task_id_str!r}", file=sys.stderr)
        sys.exit(1)

    # --- Partial-reproduction execution restriction (HPC_TASK_INCLUDE) ---
    # A partial reproduction keeps the FULL task shape (same trial_params /
    # cmd_sha) and restricts EXECUTION to a selected subset threaded through the
    # job env. A non-selected index exits 0 IMMEDIATELY — before resolving
    # kwargs, formatting result_dir, or spawning anything — so its scheduler slot
    # costs milliseconds (the idempotency skip's sibling seam). No output is
    # written, so the reduce never sees a spurious row for a skipped task.
    if not _task_is_included(task_id, os.environ):
        print(
            f"[hpc-agent] task {task_id} not in HPC_TASK_INCLUDE (partial reproduction); skipping",
            file=sys.stderr,
        )
        sys.exit(0)

    # --- Resolve task kwargs ---
    # Fast path: trial_params is serialized at submit time by compute-run-id,
    # containing the exact per-task kwargs pre-image (reserved keys stripped).
    # Using the frozen list means every array task sees the identical kwargs
    # that were hashed into cmd_sha at submission — even if tasks.py is later
    # edited, deleted, or contains stochastic module-level code. This is the
    # structural fix for per-array-task re-derivation: the sidecar carries the
    # ground truth; tasks.py is never re-executed on the cluster side.
    #
    # Fallback to tasks.py import when trial_params is absent (old sidecars
    # written before this field existed, or sidecars from _ensure_run_sidecar's
    # minimal path) preserves full backward compatibility.
    _trial_params = sidecar.get("trial_params")
    if isinstance(_trial_params, list):
        n = int(sidecar.get("task_count") or len(_trial_params))
        if not 0 <= task_id < n:
            print(
                f"[dispatch] ERROR: task_id={task_id} out of range [0, {n})",
                file=sys.stderr,
            )
            sys.exit(1)
        try:
            kwargs = _trial_params[task_id]
        except IndexError:
            print(
                f"[dispatch] ERROR: trial_params has {len(_trial_params)} entries but "
                f"task_id={task_id} (sidecar task_count={n}); re-submit to regenerate.",
                file=sys.stderr,
            )
            sys.exit(1)
        if not isinstance(kwargs, dict):
            print(
                f"[dispatch] ERROR: trial_params[{task_id}] must be a dict, "
                f"got {type(kwargs).__name__}",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        # Fallback: import tasks.py (backward compat — sidecar has no frozen manifest).
        tasks_path_str = os.environ.get("HPC_TASKS_PATH")
        tasks_path = Path(tasks_path_str) if tasks_path_str else here / "tasks.py"
        if not tasks_path.is_file():
            print(f"[dispatch] ERROR: tasks.py not found: {tasks_path}", file=sys.stderr)
            sys.exit(1)
        try:
            tasks = _load_tasks_module(tasks_path)
        except Exception as exc:
            print(f"[dispatch] ERROR: failed to import tasks.py: {exc}", file=sys.stderr)
            sys.exit(1)
        if not (hasattr(tasks, "total") and hasattr(tasks, "resolve")):
            print(
                f"[dispatch] ERROR: {tasks_path} must define total() and resolve(task_id)",
                file=sys.stderr,
            )
            sys.exit(1)
        n = int(tasks.total())
        if not 0 <= task_id < n:
            print(
                f"[dispatch] ERROR: task_id={task_id} out of range [0, {n})",
                file=sys.stderr,
            )
            sys.exit(1)
        try:
            kwargs = tasks.resolve(task_id)
        except Exception as exc:
            print(f"[dispatch] ERROR: tasks.resolve({task_id}) raised: {exc}", file=sys.stderr)
            sys.exit(1)
        if not isinstance(kwargs, dict):
            print(
                f"[dispatch] ERROR: tasks.resolve({task_id}) must return dict, "
                f"got {type(kwargs).__name__}",
                file=sys.stderr,
            )
            sys.exit(1)

    try:
        result_dir = _format_result_dir(
            result_dir_template, task_id=task_id, run_id=run_id, kwargs=kwargs
        )
    except KeyError as exc:
        print(f"[dispatch] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    # --- Idempotency skip ---
    # Helps the campus user resubmit a preempted task cleanly: if a
    # prior run already wrote ``metrics.json`` (the per-task completion
    # marker), we skip invoking the executor and exit 0. The combiner
    # picks up the existing output on the next wave; the agent harness
    # sees the same exit-0 envelope as a normal completion.
    #
    # Two opt-outs to keep the skip from masking divergence:
    #
    #   1. ``HPC_FORCE_RERUN=1`` in the spec's job_env: always run,
    #      even if metrics.json exists. Use when the user knows the
    #      executor changed but the kwargs didn't (e.g. fixed a bug
    #      and wants to re-run a campaign without bumping the version).
    #
    #   2. Auto-bypass on ``cmd_sha`` mismatch: each successful run
    #      stamps ``<result_dir>/.hpc_cmd_sha`` with this submission's
    #      cmd_sha. On re-entry, if the stamped sha differs from the
    #      sidecar's cmd_sha, the task list materially changed (code
    #      or kwargs); the stale metrics.json is from a different
    #      experiment and must not be reused. Re-run.
    #
    # Defensive: a 0-byte ``metrics.json`` (e.g. crashed mid-write) does
    # NOT trigger the skip — the user must be able to re-run.
    #
    # NFS staleness (A-M2): plain ``stat().st_size`` over NFS can return
    # a stale or partial size from the client cache; a concurrent writer
    # (a still-running prior submission of the same task_id) could
    # otherwise trigger a premature skip. We open the file and read the
    # first byte, which forces the NFS client to revalidate the inode
    # via a GETATTR/READ round-trip. The read also catches the 0-byte
    # case in the same call. Cheap (one byte, no JSON parse) and
    # contract-tight: a metrics.json file that opens and yields ≥1 byte
    # is by construction non-empty as seen by *us*, not the cache.
    force_rerun = os.environ.get("HPC_FORCE_RERUN", "").strip() == "1"
    current_cmd_sha = sidecar.get("cmd_sha")
    cmd_sha_marker = Path(result_dir) / ".hpc_cmd_sha"
    cmd_sha_changed = False
    # Initialize unconditionally so the diagnostic at line ~403 never
    # raises NameError when current_cmd_sha is falsy.
    prior_cmd_sha = ""
    if current_cmd_sha and cmd_sha_marker.is_file():
        try:
            prior_cmd_sha = cmd_sha_marker.read_text(encoding="utf-8").strip()
        except OSError:
            prior_cmd_sha = ""
        if prior_cmd_sha and prior_cmd_sha != current_cmd_sha:
            cmd_sha_changed = True

    metrics_path = Path(result_dir) / "metrics.json"
    already_complete = False
    if metrics_path.is_file():
        try:
            with open(metrics_path, "rb") as fh:
                already_complete = bool(fh.read(1))
        except OSError:
            already_complete = False
    if already_complete and not force_rerun and not cmd_sha_changed:
        print(
            f"[hpc-agent] task {task_id} already complete (metrics.json found); skipping",
            file=sys.stderr,
        )
        sys.exit(0)
    if already_complete and cmd_sha_changed:
        print(
            f"[hpc-agent] task {task_id} metrics.json exists but cmd_sha changed "
            f"(prior={prior_cmd_sha[:8]}, current={current_cmd_sha[:8]}); re-running",
            file=sys.stderr,
        )
        # Quarantine the PREVIOUS experiment's metrics.json before re-running.
        # metrics.json is the per-task completion marker: left in place, a
        # failed (or still-running) new attempt lets status/combiner count the
        # stale file as THIS run's completed result. Rename — never delete —
        # so the evidence survives for forensics, under the ``_wip_*`` naming
        # family every result scanner (check_results*, the combiner's
        # exact-name lookup) already skips, with the same ``time_ns()``
        # disambiguation the preserved-failed-WIP rename uses. Best-effort:
        # on a rename failure the stale file stays exactly as before this
        # guard existed, so warn rather than abort the re-run.
        stale_target = os.path.join(
            result_dir, f"_wip_{task_id}_stale_metrics_{time.time_ns()}.json"
        )
        try:
            os.replace(metrics_path, stale_target)
            print(f"[dispatch] quarantined stale metrics.json at {stale_target}")
        except OSError as exc:
            print(
                f"[dispatch] WARN: could not quarantine stale metrics.json {metrics_path}: {exc}",
                file=sys.stderr,
            )
    elif already_complete and force_rerun:
        print(
            f"[hpc-agent] task {task_id} metrics.json exists but HPC_FORCE_RERUN=1; re-running",
            file=sys.stderr,
        )

    # --- Install SIGTERM trap ---
    # Most preemption scenarios send SIGTERM 30-60s before SIGKILL.
    # Trap it so we can mark the run as bumped (not failed) in the
    # sidecar and forward a clean SIGINT to the executor subprocess
    # before the cluster kills us.
    # Setting HPC_PREEMPT_GRACE_SEC=0 means no grace — the SIGINT we
    # forward to the executor must complete its except blocks in
    # microseconds or its work is lost. Most campus users want the
    # default 25s so the executor's atomic-write contract has time to
    # land on disk before the cluster's SIGKILL arrives. A non-integer
    # value (or a missing one) falls back to the default rather than
    # erroring out — survival over strictness during the preemption
    # window.
    try:
        grace_sec = int(os.environ.get("HPC_PREEMPT_GRACE_SEC") or _DEFAULT_PREEMPT_GRACE_SEC)
    except ValueError:
        grace_sec = _DEFAULT_PREEMPT_GRACE_SEC
    child_holder: list = [None]
    _install_preemption_handler(
        sidecar_path=sidecar_path,
        task_id=task_id,
        child_holder=child_holder,
        grace_sec=grace_sec,
    )

    # --- WIP / atomic-promote (preserved from prior dispatcher) ---
    # MapReduce correctness guarantee: write to a temporary work-in-progress
    # directory, then atomically promote files on success. If the task
    # crashes mid-write, partial output stays in _wip_ and never pollutes
    # the final result directory.
    os.makedirs(result_dir, exist_ok=True)
    wip_dir = os.path.join(result_dir, f"_wip_{task_id}")

    if os.path.isdir(wip_dir):
        # On retry, preserve prior failed WIP for forensic inspection.
        # ``time.time_ns()`` (nanosecond) instead of ``int(time.time())``
        # — two retries within the same wall-clock second would
        # otherwise produce the same stale_target and the second
        # ``os.rename`` would fail with ENOTEMPTY, falling through to
        # the ``rmtree`` cleanup path that destroys the second
        # failure's forensic state.
        stale_target = os.path.join(result_dir, f"_wip_{task_id}_failed_{time.time_ns()}")
        try:
            os.rename(wip_dir, stale_target)
            print(f"[dispatch] preserved prior failed WIP at {stale_target}/")
        except OSError as exc:
            # Fall through with the stale WIP still in place would mix
            # leftover files from the prior run with this task's outputs
            # and corrupt the atomic promote — try a forced cleanup
            # instead. If even that fails, abort dispatch rather than
            # write into a polluted directory.
            print(
                f"[dispatch] WARN: could not preserve stale WIP {wip_dir}: {exc}; "
                f"removing it instead",
                file=sys.stderr,
            )
            try:
                shutil.rmtree(wip_dir)
            except OSError as exc2:
                print(
                    f"[dispatch] FATAL: could not clean stale WIP {wip_dir}: {exc2}",
                    file=sys.stderr,
                )
                sys.exit(1)

    os.makedirs(wip_dir, exist_ok=True)

    env = dict(os.environ)
    env["RESULT_DIR"] = wip_dir
    env["HPC_RESULT_DIR"] = wip_dir
    env["HPC_TASK_ID"] = str(task_id)
    env["HPC_RUN_ID"] = run_id

    # #294: checkpoints go in a STABLE per-task dir — the FINAL result dir, not
    # the WIP dir (which is renamed to _wip_*_failed_* / recreated on retry). A
    # killed run's checkpoints therefore survive to the resubmit. Always exported
    # so any checkpointing executor writes here; the executor's checkpoint helper
    # prefers HPC_CHECKPOINT_DIR.
    checkpoint_dir = os.path.join(result_dir, "_checkpoints")
    env["HPC_CHECKPOINT_DIR"] = checkpoint_dir
    # On `resubmit --from-checkpoint` (the dispatcher sees HPC_RESUME_FROM_CHECKPOINT=1
    # forwarded in the batch job_env), find the latest checkpoint and hand the
    # executor a concrete resume point as HPC_RESUME_FROM. No checkpoint → leave it
    # unset, and the run starts fresh (the flag is best-effort, never fatal).
    if os.environ.get("HPC_RESUME_FROM_CHECKPOINT", "").strip() == "1":
        latest_ckpt = _latest_checkpoint(checkpoint_dir)
        if latest_ckpt:
            env["HPC_RESUME_FROM"] = latest_ckpt
            print(f"[dispatch] resuming task {task_id} from checkpoint {latest_ckpt}")
        else:
            print(
                f"[dispatch] --from-checkpoint set but no checkpoint under "
                f"{checkpoint_dir}; starting task {task_id} fresh",
                file=sys.stderr,
            )
    # Kwarg export contract. Each kwarg ships as ``HPC_KW_<KEY>`` always
    # — namespaced, collision-free. The bare-uppercase ``<KEY>`` form is
    # the legacy contract (kept default-on for back-compat) and is the
    # single biggest fidelity-vs-serial risk: a kwarg named ``home`` or
    # ``path`` silently overwrites $HOME or $PATH for the executor's
    # process, changing import resolution, dataset paths, etc.
    #
    # Setting ``HPC_KW_NAMESPACE_ONLY=1`` in the spec's ``job_env``
    # disables the bare-uppercase form, exporting only ``HPC_KW_*``.
    # Recommended for new campaigns; existing campaigns that read bare
    # uppercase from inside the executor must update first.
    namespace_only = os.environ.get("HPC_KW_NAMESPACE_ONLY", "").strip() == "1"
    for key, value in kwargs.items():
        s = str(value)
        env[f"HPC_KW_{key.upper()}"] = s
        if not namespace_only:
            env[key.upper()] = s

    # Service-dependency passthrough (#231 Tier 1): an externally-provisioned
    # service address recorded on the sidecar travels to the job as the JSON
    # ``HPC_SERVICE_ENV`` var; thread each entry into the task env as a
    # namespaced ``HPC_SERVICE_<KEY>`` (collision-free, like HPC_KW_*). Absent
    # var → clean no-op for sweeps with no service dependency.
    raw_service_env = os.environ.get("HPC_SERVICE_ENV", "").strip()
    if raw_service_env:
        try:
            service_env = json.loads(raw_service_env)
        except json.JSONDecodeError as exc:
            print(f"[dispatch] WARN: ignoring malformed HPC_SERVICE_ENV: {exc}", file=sys.stderr)
        else:
            if isinstance(service_env, dict):
                _inject_service_env(env, service_env)

    # #293: a multi-rank job prefixes the per-task command with the launcher
    # (srun/mpirun/aprun) so this single dispatcher spawns N coordinated ranks
    # of the compute. No-op (empty prefix) for an ordinary single-process task.
    launch_prefix = _mpi_launch_prefix(env)
    if launch_prefix:
        executor = f"{launch_prefix} {executor}"

    # Defense-in-depth for #195: warn if the command references an HPC_KW_* var
    # the kwargs never produced (would expand to empty and fail the task). The
    # env is fully built above, so this is the authoritative diff point.
    _warn_unset_kwarg_refs(executor, env)

    print(f"[dispatch] task_id={task_id} run_id={run_id} result_dir={result_dir}")
    print(f"[dispatch] cmd={executor}")

    # Use Popen (not subprocess.run) so the SIGTERM handler can reach
    # the child via *child_holder* and forward SIGINT during the
    # cluster's preemption grace window.
    #
    # preexec_fn=os.setpgrp makes the executor its OWN process-group
    # leader (separate from the dispatcher's pgid). This isolates the
    # child from any signals the scheduler sends to the dispatcher's
    # pgroup so the dispatcher's SIGTERM handler can manage cleanup
    # explicitly. Trade-off: an A-H1 race between Popen() returning and
    # ``child_holder[0]`` assignment leaves the child unreachable by
    # the handler (its pgid is unknown to us); the handler logs and
    # exits, and cgroup teardown reaps the orphan.
    #
    # stderr is captured via a PIPE pumped by a daemon thread that relays
    # every byte straight through to our own stderr (so the cluster job
    # log is unchanged) AND retains a bounded tail. On a non-zero exit the
    # tail is persisted into the failure dir (#161) so the loop is
    # diagnosable instead of leaving an empty ``_wip_*_failed_*``. stdout
    # stays inherited — capturing it too would buffer potentially large
    # task output for no diagnostic gain.
    started_at_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    started_at_mono = time.monotonic()
    stderr_tail_buf: list[bytes] = []
    stderr_tail_lock = threading.Lock()
    # preexec_fn is POSIX-only and ``os.setpgrp`` does not exist on Windows.
    # The dispatcher is deployed to and runs on the cluster (Linux), so this
    # guard is a no-op there; it only keeps local dispatch / the Windows test
    # lane (#163) from AttributeError-ing on ``os.setpgrp``.
    popen_kwargs: dict = {}
    if hasattr(os, "setpgrp"):
        popen_kwargs["preexec_fn"] = os.setpgrp
    child = subprocess.Popen(
        executor,
        shell=True,
        env=env,
        stderr=subprocess.PIPE,
        **popen_kwargs,
    )
    child_holder[0] = child
    pump = threading.Thread(
        target=_pump_stderr,
        kwargs={
            "pipe": child.stderr,
            "tail_buf": stderr_tail_buf,
            "tail_lock": stderr_tail_lock,
            "max_bytes": _STDERR_TAIL_BYTES,
        },
        daemon=True,
    )
    pump.start()
    returncode = child.wait()
    # Give the pump a brief window to drain any stderr still buffered in
    # the pipe after the child exits. Bounded so a wedged reader can never
    # stall the dispatcher's own exit — the tail is best-effort forensics.
    pump.join(timeout=2.0)
    with stderr_tail_lock:
        stderr_tail = b"".join(stderr_tail_buf)[-_STDERR_TAIL_BYTES:]
    elapsed_sec = max(0, int(round(time.monotonic() - started_at_mono)))
    ended_at_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    if returncode == 0:
        # Walk the WIP tree recursively so an executor that writes
        # nested subdirs (e.g. ``per_seed/seed_0/metric.csv``) promotes
        # correctly. A flat ``os.listdir`` + ``os.replace`` would have
        # tried to rename the subdir over an existing result-side
        # subdir on retry and failed with ENOTEMPTY.
        promote_pairs: list[tuple[str, str]] = []
        for root, _dirs, fnames in os.walk(wip_dir):
            for fname in fnames:
                src = os.path.join(root, fname)
                rel = os.path.relpath(src, wip_dir)
                promote_pairs.append((src, rel))
        # Sort: metrics.json at the top level last (it's the
        # idempotency marker); everything else alphabetically.
        promote_pairs.sort(key=lambda pair: (pair[1] == "metrics.json", pair[1]))
        if not promote_pairs:
            # #16 (proving run #5): exit 0 but the WIP result dir is EMPTY —
            # the executor produced NO files. Under WIP/atomic-promote
            # semantics "produced a result" == "wrote at least one file to
            # $RESULT_DIR"; there is nothing to promote here, so promoting it
            # as a success is a FALSE GREEN (the canary passes on the 1-task
            # probe, the whole array runs, and only the harvest finds nothing
            # to reduce). The usual cause is an executor whose ``__main__``
            # calls the function and only ``print()``s the result, bypassing
            # the framework's result-writer (``compute(args)`` /
            # ``write_metrics``). Convert to a task FAILURE — the same
            # non-zero-exit + failure-capture + preserved-WIP path a real
            # crash takes — so the reporter counts it failed and the canary
            # catches it BEFORE the main array pays for N tasks. A distinct,
            # terminal exit code (retrying a no-output executor is futile).
            returncode = _EXIT_NO_OUTPUT
            print(
                f"[dispatch] FAILED (exit {returncode}): executor exited 0 but wrote NO "
                f"files to $RESULT_DIR ({wip_dir}) — nothing to promote. An executor must "
                "write its per-task result (e.g. metrics.json) to $RESULT_DIR; a __main__ "
                "that only prints the result bypasses the framework's result-writer "
                "(compute(args) / write_metrics). Treating as a task failure.",
                file=sys.stderr,
            )
            _write_failure_capture(
                wip_dir,
                task_id=task_id,
                returncode=returncode,
                executor=executor,
                stderr_tail=stderr_tail,
            )
        else:
            # Promote: atomically move each output file to the final directory.
            # metrics.json is the idempotency marker — if the process is
            # killed mid-promotion, a half-promoted task with metrics.json
            # already in place would be skipped on retry and the missing
            # outputs would never be recovered. metrics.json was sorted last
            # above so a crash before that point leaves the task obviously
            # incomplete.
            for src, rel in promote_pairs:
                dst = os.path.join(result_dir, rel)
                parent = os.path.dirname(dst)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                os.replace(src, dst)
            shutil.rmtree(wip_dir, ignore_errors=True)
            # Stamp this submission's cmd_sha so a subsequent re-entry can
            # detect "code or kwargs changed since this result was written"
            # and bypass the metrics.json idempotency skip. Best-effort: a
            # write failure here MUST NOT change the dispatcher's exit code
            # (the task succeeded; the marker is only a hint for next time).
            if current_cmd_sha:
                try:
                    Path(result_dir, ".hpc_cmd_sha").write_text(current_cmd_sha, encoding="utf-8")
                except OSError as exc:
                    print(
                        f"[dispatch] WARN: failed to stamp .hpc_cmd_sha in {result_dir}: {exc}",
                        file=sys.stderr,
                    )
    else:
        print(
            f"[dispatch] FAILED (exit {returncode}), partial output preserved in {wip_dir}",
            file=sys.stderr,
        )
        # Capture WHY into the failure dir. Before this, a failed dispatch
        # left an empty ``_wip_*_failed_*`` with no clue (#161); now the
        # exit code, the per-task command, and the tail of the executor's
        # stderr land in ``_hpc_dispatch_error.log`` for the campus user
        # and the agent's failure-classifier.
        _write_failure_capture(
            wip_dir,
            task_id=task_id,
            returncode=returncode,
            executor=executor,
            stderr_tail=stderr_tail,
        )

    # Write per-task runtime sidecar — feeds the warm-axis-picker via
    # the local-side ingest pipeline (combiner aggregates these per
    # wave; aggregate_flow rsync_pulls and ingests them into
    # ``runtimes/<profile>.<cluster>.json`` via append_sample).
    # Best-effort: a write failure here MUST NOT change the dispatcher's
    # exit code (the task itself succeeded or failed on its own merits).
    try:
        runtime_payload = {
            "task_id": int(task_id),
            "run_id": run_id,
            "started_at": started_at_iso,
            "ended_at": ended_at_iso,
            "elapsed_sec": int(elapsed_sec),
            "exit_code": int(returncode),
            "node": (
                os.environ.get("SLURMD_NODENAME")
                or os.environ.get("HOSTNAME")
                or os.environ.get("HOST")
                or ""
            ),
            # gpu_preamble.sh uses ${HPC_GPU_TYPE+set} to distinguish
            # *unset* (auto-detect) from *explicitly empty* (operator
            # opt-out: "leave it unset / skip auto-detect"). The naive
            # ``or`` chain treats "" as falsy and falls through to
            # $SLURM_JOB_PARTITION, silently overriding the operator's
            # intent. Honor presence-not-truthiness here.
            "gpu_type": (
                os.environ["HPC_GPU_TYPE"]
                if "HPC_GPU_TYPE" in os.environ
                else (os.environ.get("SLURM_JOB_PARTITION") or "")
            ),
            # axis_bindings = the dict the warm picker groups by. ``kwargs``
            # is whatever ``tasks.resolve(task_id)`` returned — exactly the
            # axis values the user's tasks.py exposed.
            "axis_bindings": {str(k): v for k, v in kwargs.items()},
        }
        runtime_path = os.path.join(result_dir, "_runtime.json")
        # Atomic-ish write: tempfile + replace. Worst case (crash mid-write)
        # leaves no _runtime.json — the ingest path treats that as "no
        # sample for this task", which is the right fallback.
        fd, tmp = tempfile.mkstemp(prefix="_runtime.", suffix=".tmp", dir=result_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                # default=str so numpy ints / datetimes / Path objects /
                # any other non-JSON-native value coming back from
                # ``tasks.resolve(task_id)`` falls back to repr instead of
                # silently nuking the runtime sample. The warm picker
                # treats axis_bindings as opaque keys for grouping —
                # consistent string repr is enough; native typing isn't
                # required.
                json.dump(runtime_payload, fh, sort_keys=True, default=str)
            os.replace(tmp, runtime_path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    except Exception as exc:  # noqa: BLE001 — sidecar is best-effort
        print(
            f"[dispatch] WARN: failed to write _runtime.json for task {task_id}: {exc}",
            file=sys.stderr,
        )

    sys.exit(returncode)


if __name__ == "__main__":
    main()
