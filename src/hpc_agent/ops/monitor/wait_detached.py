"""``wait-detached`` — block until a detached worker exits (harness bridge).

Detach-by-contract (design §3) hands slow blocks to a raw-``Popen`` worker so
the chat is never held — but that severs the harness's completion-notification
channel: the driving agent has nothing to await, so it falls back to timed
``/loop`` polling (proving-run-3 finding (b)): guessed cadences, cache burn,
and up to a full poll interval of dead air after the brief is ready.

This verb is the bridge: a BLOCKING local wait on the worker's lease pid
(``_kernel/lifecycle/detached.py`` writes ``_detached/<block>-<run_id>.lease.json``
with the launched pid; ``pid_alive`` probes it). The agent launches
``hpc-agent wait-detached --spec <file with {"run_id": ...}>`` (a file path —
never inline JSON on the command line) through the harness's native
backgrounding (Claude Code ``run_in_background``) and the harness wakes it
exactly once, when this process exits — event-driven, no polling, no SSH
(purely local pid probes).

Deliberately NOT in the curated MCP catalog (its Result carries no
``next_block``, so the derived catalog excludes it): the MCP server dispatches
tools in-process and synchronously — a multi-hour blocking tool call would
wedge the server. This is a CLI-fallback-only affordance by design; the skills
route it through backgrounded Bash. Curated exclusion only covers the curated
catalog, so the ``full``/``tiered`` catalogs (where this verb is otherwise
invocable) are backstopped at the seam:
``_kernel/extension/mcp_server.py::_refuse_blocking_over_mcp`` refuses it
outright there (``_BLOCKING_WAIT_VERBS``), pointing callers at ``poll-detached``
or backgrounded Bash.

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


def _newest_lease(detached_dir: Path, run_id: str, block: str | None) -> dict[str, Any] | None:
    """The newest lease dict for ``(run_id[, block])`` regardless of pid liveness.

    Distinct from :func:`_live_lease` (which requires a LIVE pid): this is the
    read-side lookup used AFTER the wait to recover where the worker's journal
    home / experiment dir live, so ``wait-detached`` can read the exited worker's
    recorded terminal even on the ``no_live_worker`` return (the worker already
    gone). A corrupt lease is skipped, never fatal.
    """
    pattern = f"{block}-{run_id}.lease.json" if block else f"*-{run_id}.lease.json"
    newest: dict[str, Any] | None = None
    newest_mtime = -1.0
    for lease_path in detached_dir.glob(pattern):
        try:
            loaded: Any = json.loads(lease_path.read_text(encoding="utf-8"))
            mtime = lease_path.stat().st_mtime
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(loaded, dict) and mtime > newest_mtime:
            newest, newest_mtime = loaded, mtime
    return newest


def _terminal_pointers(
    lease: dict[str, Any] | None, run_id: str, block: str | None
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Read the exited worker's ``(brief, relay, next_verb)`` from its terminal.

    Reads the block-terminal record (the FULL ``SubmitBlockResult`` dump the
    worker recorded on the way out — it carries the brief, the code-rendered
    relay, and the next_block hint) for ``(run_id, block)``, falling back to the
    §5 pending-decision marker's brief + resume-cursor when no terminal exists.
    The experiment dir is recovered from the lease (stamped at spawn), or the cwd
    when the lease predates that field / is absent. Fail-open: any read problem
    yields all-``None`` — the wake-up payload is a convenience, never load-bearing
    (the journal still carries the truth).
    """
    block_for_read = block
    experiment_dir: Path | None = None
    if isinstance(lease, dict):
        lease_block = lease.get("block")
        block_for_read = block or (lease_block if isinstance(lease_block, str) else None)
        ed = lease.get("experiment_dir")
        if isinstance(ed, str) and ed:
            experiment_dir = Path(ed)
    if experiment_dir is None:
        experiment_dir = Path.cwd()
    if not block_for_read:
        return None, None, None
    try:
        from hpc_agent.state.block_terminal import read_terminal_with_fallback

        record = read_terminal_with_fallback(experiment_dir, run_id, block_for_read)
    except Exception:  # noqa: BLE001 — the payload is a convenience, never load-bearing
        record = None
    result = record.get("result") if isinstance(record, dict) else None
    if isinstance(result, dict):
        raw_brief = result.get("brief")
        brief = raw_brief if isinstance(raw_brief, dict) else None
        raw_relay = result.get("relay")
        relay = raw_relay if isinstance(raw_relay, str) and raw_relay else None
        nb = result.get("next_block")
        next_verb = nb.get("verb") if isinstance(nb, dict) else None
        return brief, relay, next_verb if isinstance(next_verb, str) else None
    # No terminal recorded — fall back to the §5 pending-decision marker (L2: the
    # worker parked itself, so the marker's brief + cursor are the wake payload).
    try:
        from hpc_agent.state.journal import read_pending_decision

        marker = read_pending_decision(run_id, experiment_dir=experiment_dir)
    except Exception:  # noqa: BLE001 — convenience read, never load-bearing
        marker = {}
    if not marker:
        return None, None, None
    brief = marker.get("brief") if isinstance(marker.get("brief"), dict) else None
    cursor = marker.get("resume_cursor")
    next_verb = cursor.get("next_verb") if isinstance(cursor, dict) else None
    return brief, None, next_verb if isinstance(next_verb, str) else None


def _live_lease(detached_dir: Path, run_id: str, block: str | None) -> dict[str, Any] | None:
    """The first lease for ``(run_id[, block])`` whose pid is alive, else None.

    Lease files are ``<block>-<run_id>.lease.json``. A corrupt/partial lease
    (mid-write, stale schema) is skipped, never fatal — an unreadable lease
    must not strand the waiter; the ``no_live_worker`` outcome + journal state
    remain the truthful answer.
    """
    from hpc_agent._kernel.lifecycle.detached import pid_alive

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
        if isinstance(pid, int) and pid > 0 and pid_alive(pid):
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
    from hpc_agent._kernel.lifecycle.detached import pid_alive
    from hpc_agent.state.run_record import current_homedir

    detached_dir = current_homedir() / "_detached"
    started = time.monotonic()
    have_dir = detached_dir.is_dir()

    lease = _live_lease(detached_dir, spec.run_id, spec.block) if have_dir else None
    if lease is None:
        # No LIVE worker: it either already exited (its recorded terminal is the
        # wake payload) or was never launched. Recover the pointers from whatever
        # lease is on disk (dead pid) so an already-done worker still hands back
        # its brief/relay/next_verb.
        read_lease = _newest_lease(detached_dir, spec.run_id, spec.block) if have_dir else None
        brief, relay, next_verb = _terminal_pointers(read_lease, spec.run_id, spec.block)
        return WaitDetachedResult(
            outcome="no_live_worker",
            run_id=spec.run_id,
            block=(read_lease.get("block") if isinstance(read_lease, dict) else None) or spec.block,
            pid=None,
            log_path=read_lease.get("log_path") if isinstance(read_lease, dict) else None,
            waited_sec=0.0,
            brief=brief,
            relay=relay,
            next_verb=next_verb,
        )

    pid = int(lease["pid"])
    while pid_alive(pid):
        if time.monotonic() - started >= spec.timeout_sec:
            # Still alive at the budget — no terminal yet, so no wake payload.
            return WaitDetachedResult(
                outcome="timeout",
                run_id=spec.run_id,
                block=lease.get("block") or spec.block,
                pid=pid,
                log_path=lease.get("log_path"),
                waited_sec=round(time.monotonic() - started, 3),
            )
        time.sleep(spec.poll_interval_sec)

    # The worker exited: it parked itself on the way out (L2), so its recorded
    # terminal carries the decision brief the woken agent needs directly.
    brief, relay, next_verb = _terminal_pointers(lease, spec.run_id, spec.block)
    return WaitDetachedResult(
        outcome="worker_exited",
        run_id=spec.run_id,
        block=lease.get("block") or spec.block,
        pid=pid,
        log_path=lease.get("log_path"),
        waited_sec=round(time.monotonic() - started, 3),
        brief=brief,
        relay=relay,
        next_verb=next_verb,
    )
