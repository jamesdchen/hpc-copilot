"""Deterministic detached drive mode — run the lifecycle composite in a
DETACHED CLI subprocess, never a ``claude -p`` worker.

The connection-storm hazard (the 0.10.63 ban) was *an LLM in the connection
loop*: ``hpc-agent run --workflow status`` spawns a ``claude -p --bare`` worker
to **drive** the wait-until-terminal poll; the worker auto-backgrounds at 2 min,
ends its turn mid-poll, and a fallback inline subagent then retries SSH in prose
for ~21 minutes. The deterministic composite it was driving
(``status-pipeline`` → ``monitor_flow``) already runs the whole poll loop in
plain code with the connection owned by a single process and the model out of
the loop — the principle :mod:`hpc_agent.infra.retry` states. The miss was the
*drive layer*: the model still sat on top of it.

This module carries that principle all the way to the drive layer. The
**detached** drive mode launches the deterministic composite as a DETACHED
subprocess of the ``hpc-agent`` CLI (NOT a ``claude -p --bare`` worker): one
deterministic process owns the connection and runs to terminal, while the
orchestrator learns the outcome by reading the journal
(:mod:`hpc_agent.state.journal_poll`) — never by spawning an LLM to poke SSH.
This mirrors DPDispatcher's "submit and poke until they finish" loop and
jobflow-remote's Runner daemon: the lifecycle runs to completion in a
deterministic process, the orchestrator reads state from disk.

Scope of the landed slice: the ``status`` workflow's blocking wait-until-terminal
path — the exact lifecycle the LLM sat in. ``submit`` / ``aggregate`` keep the
default ``claude -p --bare`` worker; see the module-level ``_SUPPORTED`` set and
``docs/workflows/code-driven-orchestration.md`` for the extension plan.

Opt-in only. The default stays the proven ``--bare`` worker (``hpc-agent run``):
detached mode is selected with ``--detached`` or ``HPC_AGENT_DRIVE=detached``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from typing import Any

__all__ = [
    "DetachedLaunch",
    "DriveModeError",
    "detached_drive_supported",
    "build_status_pipeline_spec",
    "launch_status_pipeline_detached",
]

# Workflows the detached deterministic runner can drive today. ``status`` is the
# wait-until-terminal poll the LLM used to sit in — the ban cause. ``submit`` and
# ``aggregate`` are deferred (see the module docstring / design doc): they keep
# the default worker.
_SUPPORTED = frozenset({"status"})


class DriveModeError(ValueError):
    """The detached drive mode was requested for an unsupported shape."""


@dataclass(frozen=True)
class DetachedLaunch:
    """Handle for a launched detached deterministic runner.

    ``run_id`` is what the orchestrator polls the journal for
    (:func:`hpc_agent.state.journal_poll.poll_until_terminal`). ``pid`` is the
    detached subprocess id (informational — the orchestrator does NOT wait on
    it; it reads the journal). ``log_path`` is where the runner's stdout/stderr
    (the composite's JSON envelope + any diagnostics) are captured, so a
    post-mortem never needs to re-open SSH.
    """

    run_id: str
    pid: int
    log_path: str
    argv: list[str]


def detached_drive_supported(workflow: str, fields: dict[str, Any]) -> bool:
    """Whether *workflow* + *fields* can be driven deterministically detached.

    True only for the ``status`` workflow on its blocking wait path (the
    lifecycle the LLM sat in). A snapshot status (``blocking`` false/absent)
    has no loop to drive, so it is NOT a detached candidate — the caller just
    runs ``hpc-agent status`` once.
    """
    if workflow not in _SUPPORTED:
        return False
    if workflow == "status":
        return bool(fields.get("blocking"))
    return False


def build_status_pipeline_spec(fields: dict[str, Any]) -> dict[str, Any]:
    """Map ``run --workflow status`` *fields* to a ``status-pipeline`` spec dict.

    The ``status`` worker prompt's wait path runs ``hpc-agent status-pipeline``
    with a spec embedding the monitor spec under ``monitor`` (``run_id`` + poll
    cadence + wall-clock budget). The detached runner builds that spec in code
    from the same interview fields, so no LLM renders it.

    Required field: ``run_id``. Optional pass-throughs (each defaulted by the
    ``MonitorFlowSpec`` model when omitted): ``poll_interval_seconds``,
    ``wall_clock_budget_seconds``, ``auto_combine_waves``, ``combiner_max_retries``,
    ``file_glob``. Raises :class:`DriveModeError` when ``run_id`` is absent — the
    detached runner cannot poll a run it can't name.
    """
    run_id = fields.get("run_id")
    if not run_id or not isinstance(run_id, str):
        raise DriveModeError(
            "detached status drive requires a string 'run_id' in fields; got "
            f"{run_id!r}. The detached deterministic runner polls the journal by "
            "run_id, so it must be known before launch (the snapshot 'status' "
            "path discovers it, but the wait path is keyed by it)."
        )
    monitor: dict[str, Any] = {"run_id": run_id}
    for key in (
        "poll_interval_seconds",
        "wall_clock_budget_seconds",
        "auto_combine_waves",
        "combiner_max_retries",
        "file_glob",
    ):
        if key in fields and fields[key] is not None:
            monitor[key] = fields[key]
    return {"monitor": monitor}


def _detach_popen_kwargs() -> dict[str, Any]:
    """Platform flags that fully detach the child from this process.

    The runner must OUTLIVE the orchestrator: if the caller's session ends
    (the exact failure that killed the auto-backgrounded ``submit-pipeline`` in
    0.10.63 — "the harness killed the pipeline ~1s after the main qsub"), the
    deterministic poll must keep owning the connection to terminal. POSIX:
    ``start_new_session=True`` makes the child a session+group leader so it does
    not receive the parent's SIGHUP/SIGINT. Windows: ``DETACHED_PROCESS |
    CREATE_NEW_PROCESS_GROUP`` so it has no console and ignores the parent's
    Ctrl-C/console-close.
    """
    if sys.platform == "win32":
        flags = 0
        # These attributes exist only on Windows; guard for the type checker.
        flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return {"creationflags": flags}
    return {"start_new_session": True}


def launch_status_pipeline_detached(
    *,
    experiment_dir: str,
    fields: dict[str, Any],
    hpc_agent_bin: str | None = None,
) -> DetachedLaunch:
    """Launch ``hpc-agent status-pipeline`` as a DETACHED CLI subprocess.

    Writes the ``status-pipeline`` spec (built by :func:`build_status_pipeline_spec`)
    to the journal home's ``_detached/`` dir, then ``Popen``s the composite
    fully detached (no stdin; stdout/stderr → a log file under the same dir). The
    composite owns the SSH connection and runs ``monitor_flow`` to terminal in
    plain code, writing the journal as it goes; the orchestrator polls the
    journal (:func:`hpc_agent.state.journal_poll.poll_until_terminal`) for the
    outcome. NO ``claude -p`` worker is spawned anywhere on this path.

    The journal home (``HPC_JOURNAL_DIR`` / ``~/.claude/hpc``) is the write
    target — always present/writable, redirectable, and never pollutes the
    experiment tree (the same target ``cli/spawn._maybe_persist_inline_prompt``
    uses).

    *hpc_agent_bin* overrides the launched binary (default: ``hpc-agent`` on
    PATH); a test passes a stub. Raises :class:`DriveModeError` via
    :func:`build_status_pipeline_spec` when ``run_id`` is missing.
    """
    from hpc_agent.state.run_record import _current_homedir

    spec = build_status_pipeline_spec(fields)
    run_id: str = spec["monitor"]["run_id"]

    detached_dir = _current_homedir() / "_detached"
    detached_dir.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:8]
    spec_path = detached_dir / f"status-{run_id}-{token}.spec.json"
    log_path = detached_dir / f"status-{run_id}-{token}.log"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    argv = [
        hpc_agent_bin or "hpc-agent",
        "status-pipeline",
        "--spec",
        str(spec_path),
        "--experiment-dir",
        experiment_dir,
    ]
    # Detached: no stdin (DEVNULL so a poll can never block on a tty), and
    # stdout/stderr captured to a log so the composite's envelope + diagnostics
    # survive without the orchestrator reading them live. The orchestrator
    # learns the outcome from the JOURNAL, not this pipe.
    log_handle = open(log_path, "w", encoding="utf-8")  # noqa: SIM115 — handed to the child
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            cwd=experiment_dir,
            env={**os.environ},
            **_detach_popen_kwargs(),
        )
    finally:
        # The child inherits its own dup'd fd; this process closes its copy so
        # it isn't holding the log open for the runner's whole lifetime.
        log_handle.close()

    return DetachedLaunch(
        run_id=run_id,
        pid=proc.pid,
        log_path=str(log_path.resolve()),
        argv=argv,
    )
