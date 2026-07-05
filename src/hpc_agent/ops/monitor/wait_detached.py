"""``wait-detached`` — block until a detached worker exits (harness bridge).

Detach-by-contract (design §3) hands slow blocks to a raw-``Popen`` worker so
the chat is never held — but that severs the harness's completion-notification
channel: the driving agent has nothing to await, so it falls back to timed
``/loop`` polling (proving-run-3 finding (b)): guessed cadences, cache burn,
and up to a full poll interval of dead air after the brief is ready.

This verb is the bridge: a BLOCKING local wait on the worker's lease pid
(``_kernel/lifecycle/detached.py`` writes ``_detached/<block>-<run_id>.lease.json``
with the launched pid; ``_pid_alive`` probes it). The agent launches
``hpc-agent wait-detached --spec <file with {"run_id": ...}>`` (a file path —
never inline JSON on the command line) through the harness's native
backgrounding (Claude Code ``run_in_background``) and the harness wakes it
exactly once, when this process exits — event-driven, no polling, no SSH
(purely local pid probes).

Deliberately NOT in the curated MCP catalog (its Result carries no
``next_block``, so the derived catalog excludes it): the MCP server dispatches
tools in-process and synchronously — a multi-hour blocking tool call would
wedge the server. This is a CLI-fallback-only affordance by design; the skills
route it through backgrounded Bash.

The §5 watchdog is untouched backstop: this waiter dying (session death) loses
only the notification — doctor/the watchdog still catch the run.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.queries.wait_detached import WaitDetachedInput, WaitDetachedResult
from hpc_agent.cli._dispatch import CliShape, SchemaRef


def _live_lease(detached_dir: Path, run_id: str, block: str | None) -> dict[str, Any] | None:
    """The first lease for ``(run_id[, block])`` whose pid is alive, else None.

    Lease files are ``<block>-<run_id>.lease.json``. A corrupt/partial lease
    (mid-write, stale schema) is skipped, never fatal — an unreadable lease
    must not strand the waiter; the ``no_live_worker`` outcome + journal state
    remain the truthful answer.
    """
    from hpc_agent._kernel.lifecycle.detached import _pid_alive

    pattern = f"{block}-{run_id}.lease.json" if block else f"*-{run_id}.lease.json"
    for lease_path in sorted(detached_dir.glob(pattern)):
        try:
            loaded: Any = json.loads(lease_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(loaded, dict):
            continue
        lease: dict[str, Any] = loaded
        pid = lease.get("pid")
        if isinstance(pid, int) and pid > 0 and _pid_alive(pid):
            return lease
    return None


@primitive(
    name="wait-detached",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    # A pure wait: re-running it is always safe (it writes nothing).
    idempotent=True,
    cli=CliShape(
        help=(
            "Block until the detached worker for a run exits (lease-pid wait; "
            "local, no SSH). Launch via the harness's backgrounding so the "
            "harness notifies you exactly once, when the brief is ready — "
            "never timed /loop polling."
        ),
        spec_arg=True,
        spec_model=WaitDetachedInput,
        schema_ref=SchemaRef(input="wait_detached"),
    ),
    agent_facing=True,
)
def wait_detached(*, spec: WaitDetachedInput) -> WaitDetachedResult:
    """Wait for the ``(run_id[, block])`` detached worker's pid to die.

    Returns ``worker_exited`` when the lease pid dies within the budget,
    ``no_live_worker`` immediately when no live lease exists at call time
    (worker already done, or never launched — read the journal next either
    way), and ``timeout`` when the budget elapses with the worker still
    alive (not an anomaly: long queue waits are normal; re-arm or consult
    ``status-snapshot``).
    """
    from hpc_agent._kernel.lifecycle.detached import _pid_alive
    from hpc_agent.state.run_record import _current_homedir

    detached_dir = _current_homedir() / "_detached"
    started = time.monotonic()

    lease = _live_lease(detached_dir, spec.run_id, spec.block) if detached_dir.is_dir() else None
    if lease is None:
        return WaitDetachedResult(
            outcome="no_live_worker",
            run_id=spec.run_id,
            block=spec.block,
            pid=None,
            log_path=None,
            waited_sec=0.0,
        )

    pid = int(lease["pid"])
    while _pid_alive(pid):
        if time.monotonic() - started >= spec.timeout_sec:
            return WaitDetachedResult(
                outcome="timeout",
                run_id=spec.run_id,
                block=lease.get("block") or spec.block,
                pid=pid,
                log_path=lease.get("log_path"),
                waited_sec=round(time.monotonic() - started, 3),
            )
        time.sleep(spec.poll_interval_sec)

    return WaitDetachedResult(
        outcome="worker_exited",
        run_id=spec.run_id,
        block=lease.get("block") or spec.block,
        pid=pid,
        log_path=lease.get("log_path"),
        waited_sec=round(time.monotonic() - started, 3),
    )
