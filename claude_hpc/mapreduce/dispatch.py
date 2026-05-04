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
``claude_hpc`` package.
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
# scheduler. 130 = 128 + 2 (SIGINT). claude-hpc treats the trapped
# SIGTERM as if the executor received SIGINT, so the campus user's
# agent harness sees the canonical "job interrupted" code regardless
# of which signal the scheduler actually sent — preempted runs survive
# as a clean, recognizable diagnostic instead of a noisy crash.
_EXIT_PREEMPTED = 130

# Sidecar schema versions this dispatcher accepts. Kept in sync with
# ``SIDECAR_SCHEMA_VERSION`` in ``claude_hpc/orchestrator/runs.py``. Hardcoded
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
    ``claude_hpc.*`` (cluster-side stdlib-only constraint).
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
        sidecar = json.loads(Path(sidecar_path).read_text())
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
         flow assigning into ``child_holder[0]``, falls back to
         ``killpg`` on the dispatcher's own process group — which the
         child inherits via ``preexec_fn=os.setpgrp`` — so no executor
         is ever orphaned by the race.
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
            "[claude-hpc] SIGTERM received; cluster preemption imminent",
            file=sys.stderr,
        )
        _mark_preempted_in_sidecar(sidecar_path, task_id, _utcnow_iso(), grace_sec=grace_sec)

        child = child_holder[0] if child_holder else None
        if child is not None and child.poll() is None:
            with contextlib.suppress(OSError):
                child.send_signal(signal.SIGINT)
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
                    child.terminate()
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(OSError):
                        child.kill()
                    with contextlib.suppress(Exception):
                        child.wait(timeout=2)
        else:
            # A-H1: race window. SIGTERM landed before the main flow
            # could populate child_holder[0]. The Popen() call ran with
            # preexec_fn=os.setpgrp, so the child (if it exists) is in
            # our process group; signal the whole pgid so the executor
            # gets the SIGINT even though we never saw the Popen handle.
            # Best-effort: if there genuinely is no child yet, killpg
            # is a no-op against ourselves (we're about to sys.exit
            # anyway, and SIGINT to ourselves while SIGTERM is now
            # ignored is harmless — the default Python SIGINT handler
            # raises KeyboardInterrupt which sys.exit overrides).
            with contextlib.suppress(OSError, ProcessLookupError):
                os.killpg(os.getpgid(0), signal.SIGINT)

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
        sidecar = json.loads(sidecar_path.read_text())
    except json.JSONDecodeError as exc:
        print(f"[dispatch] ERROR: failed to parse sidecar: {exc}", file=sys.stderr)
        sys.exit(1)

    schema_version = sidecar.get("sidecar_schema_version")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        print(
            f"[dispatch] ERROR: sidecar schema_version={schema_version}, "
            f"supported={list(SUPPORTED_SCHEMA_VERSIONS)}. Re-submit with current claude-hpc.",
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
    metrics_path = Path(result_dir) / "metrics.json"
    already_complete = False
    if metrics_path.is_file():
        try:
            with open(metrics_path, "rb") as fh:
                already_complete = bool(fh.read(1))
        except OSError:
            already_complete = False
    if already_complete:
        print(
            f"[claude-hpc] task {task_id} already complete (metrics.json found); skipping",
            file=sys.stderr,
        )
        sys.exit(0)

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
        stale_target = os.path.join(result_dir, f"_wip_{task_id}_failed_{int(time.time())}")
        try:
            os.rename(wip_dir, stale_target)
            print(f"[dispatch] preserved prior failed WIP at {stale_target}/")
        except OSError as exc:
            print(
                f"[dispatch] WARN: could not preserve stale WIP {wip_dir}: {exc}",
                file=sys.stderr,
            )

    os.makedirs(wip_dir, exist_ok=True)

    env = dict(os.environ)
    env["RESULT_DIR"] = wip_dir
    env["HPC_RESULT_DIR"] = wip_dir
    env["HPC_TASK_ID"] = str(task_id)
    env["HPC_RUN_ID"] = run_id
    for key, value in kwargs.items():
        s = str(value)
        env[key.upper()] = s
        env[f"HPC_KW_{key.upper()}"] = s

    print(f"[dispatch] task_id={task_id} run_id={run_id} result_dir={result_dir}")
    print(f"[dispatch] cmd={executor}")

    # Use Popen (not subprocess.run) so the SIGTERM handler can reach
    # the child via *child_holder* and forward SIGINT during the
    # cluster's preemption grace window.
    #
    # preexec_fn=os.setpgrp puts the executor in its own process group
    # rooted at the dispatcher's pgid. Closes A-H1: if SIGTERM lands
    # between Popen() returning and the line that assigns into
    # child_holder[0], the handler falls back to killpg on our pgid
    # — which the child has just joined — instead of leaving the
    # executor orphaned (inherited by init, eventually cleaned up by
    # cgroup teardown but possibly still writing output in the
    # meantime, surprising the next user of the same node).
    child = subprocess.Popen(executor, shell=True, env=env, preexec_fn=os.setpgrp)
    child_holder[0] = child
    returncode = child.wait()

    if returncode == 0:
        # Promote: atomically move each output file to the final directory.
        for fname in os.listdir(wip_dir):
            os.replace(os.path.join(wip_dir, fname), os.path.join(result_dir, fname))
        shutil.rmtree(wip_dir, ignore_errors=True)
    else:
        print(
            f"[dispatch] FAILED (exit {returncode}), partial output preserved in {wip_dir}",
            file=sys.stderr,
        )

    sys.exit(returncode)


if __name__ == "__main__":
    main()
