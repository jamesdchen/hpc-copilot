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
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

__all__ = ["main"]

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
        with os.fdopen(fd, "w") as fh:
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
    except (OSError, json.JSONDecodeError):
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

    # --- Load user tasks.py ---
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

    n = int(tasks.total())
    if not 0 <= task_id < n:
        print(
            f"[dispatch] ERROR: task_id={task_id} out of range [0, {n})",
            file=sys.stderr,
        )
        sys.exit(1)

    # --- Resolve run identity & sidecar ---
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
    except json.JSONDecodeError as exc:
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

    # --- Resolve task kwargs ---
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
    started_at_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    started_at_mono = time.monotonic()
    child = subprocess.Popen(executor, shell=True, env=env, preexec_fn=os.setpgrp)
    child_holder[0] = child
    returncode = child.wait()
    elapsed_sec = max(0, int(round(time.monotonic() - started_at_mono)))
    ended_at_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    if returncode == 0:
        # Promote: atomically move each output file to the final directory.
        # metrics.json is the idempotency marker — if the process is
        # killed mid-promotion, a half-promoted task with metrics.json
        # already in place would be skipped on retry and the missing
        # outputs would never be recovered. Move metrics.json last so a
        # crash before that point leaves the task obviously incomplete.
        #
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
            with os.fdopen(fd, "w") as fh:
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
