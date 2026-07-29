""":literal:`wait-any-detached` — block until ANY of a set of detached workers exits.

The FLEET form of ``wait-detached`` (``ops/monitor/wait_detached.py``), and the
run-queue plan's deferred §7 efficiency primitive
(``docs/plans/run-queue-placement-2026-07-28.md``, "``wait-any-detached``
(Phase 2 efficiency)").

WHY IT EXISTS. A drain pass over N running items holds N chunked
``wait-detached`` relays today — one subagent seat each, 480s per chunk —
because a single waiter watches exactly ONE ``(run_id, block)`` lease. The cost
of holding the fleet therefore scales with the fleet: held-hours × runs, paid in
small agent calls. This verb collapses that to ONE holder for the whole set: it
polls every target's lease on the same cadence and returns the moment ANY
watched worker resolves, naming which one. Cost per night becomes flat in the
number of runs, and the woken plan already knows what to act on.

PURE READ — the property that makes N concurrent waiters safe, preserved here.
Like ``wait-detached``, this verb stats/reads lease files, probes pids, and
reads already-recorded terminals; it writes NOTHING — no journal append, no
lease write, no marker, ``side_effects=[]``. Nothing about watching a worker may
perturb the worker, and two passes watching overlapping sets must never race, so
the read-only-ness is a contract, not an implementation detail
(``tests/ops/monitor/test_wait_any_detached.py`` pins it against the experiment
tree and the lease dir).

Identity and vocabulary are ``wait-detached``'s, deliberately unchanged: a
target is the same ``(run_id[, block])`` pair, and the outcome literals are the
same three (``worker_exited`` / ``no_live_worker`` / ``timeout``), so a caller
branches on ONE vocabulary whether it waited on one run or forty. The
lease-reading helpers are IMPORTED from ``wait_detached`` rather than
re-implemented — one definition of "which lease is this target's, and is it
alive".

Like ``wait-detached`` this is a BLOCKING call: launch it through the harness's
native backgrounding (Claude Code ``run_in_background``), never over the
synchronous MCP server.
"""

from __future__ import annotations

import dataclasses
import time
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.queries.wait_any_detached import (
    DetachedTargetState,
    WaitAnyDetachedInput,
    WaitAnyDetachedResult,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef

# ONE definition of "this target's lease, and is its pid alive" — the sibling
# single waiter's helpers, imported rather than forked (same package, so these
# stay package-private). Re-implementing them here would be a second definition
# of lease identity that could drift from ``wait-detached``'s the first time the
# lease filename convention changed.
from hpc_agent.ops.monitor.wait_detached import (
    _live_lease,
    _newest_lease,
    _terminal_pointers,
)

if TYPE_CHECKING:
    from pathlib import Path

    from hpc_agent._wire.queries.wait_any_detached import DetachedTarget


@dataclasses.dataclass
class _Watch:
    """One target's first-poll resolution, carried through the wait."""

    target: DetachedTarget
    # The LIVE lease found at the first poll, or None when the target had no
    # live worker then (already exited, never launched, or only corrupt leases).
    lease: dict[str, Any] | None
    pid: int | None
    exited: bool = False


def _row(
    watch: _Watch,
    detached_dir: Path,
    *,
    state: str,
    triggered: bool,
    have_dir: bool,
) -> DetachedTargetState:
    """Project one watch into its result row, reading the wake payload if resolved.

    A resolved target (``worker_exited`` / ``no_live_worker``) gets the same
    payload ``wait-detached`` hands back: the exited worker's recorded terminal
    (falling back to the §5 pending-decision marker), read fail-open. A
    ``still_running`` target has no terminal yet, so its payload stays null —
    exactly ``wait-detached``'s ``timeout`` behaviour.
    """
    target = watch.target
    lease = watch.lease
    if state == "still_running":
        return DetachedTargetState(
            run_id=target.run_id,
            block=(lease.get("block") if isinstance(lease, dict) else None) or target.block,
            pid=watch.pid,
            log_path=lease.get("log_path") if isinstance(lease, dict) else None,
            state="still_running",
            triggered=False,
        )
    # Resolved: for a worker that died DURING the wait the live lease is the
    # read handle; for one already gone at the first poll, recover whatever
    # lease is on disk (dead pid) so a late arrival still gets its brief.
    read_lease = (
        lease
        if lease is not None
        else (_newest_lease(detached_dir, target.run_id, target.block) if have_dir else None)
    )
    brief, relay, next_verb = _terminal_pointers(read_lease, target.run_id, target.block)
    return DetachedTargetState(
        run_id=target.run_id,
        block=(read_lease.get("block") if isinstance(read_lease, dict) else None) or target.block,
        pid=watch.pid,
        log_path=read_lease.get("log_path") if isinstance(read_lease, dict) else None,
        state="worker_exited" if state == "worker_exited" else "no_live_worker",
        triggered=triggered,
        brief=brief,
        relay=relay,
        next_verb=next_verb,
    )


@primitive(
    name="wait-any-detached",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    # A pure wait over a SET: it writes nothing, so re-running it — or running
    # several over overlapping sets — is always safe.
    idempotent=True,
    cli=CliShape(
        help=(
            "Block until ANY detached worker in a target set exits (lease-pid "
            "wait over N runs; local, no SSH). One held seat for a whole fleet "
            "instead of one wait-detached per run; returns the triggering "
            "target(s) plus every other target's current state."
        ),
        spec_arg=True,
        spec_model=WaitAnyDetachedInput,
        schema_ref=SchemaRef(input="wait_any_detached"),
    ),
    agent_facing=True,
)
def wait_any_detached(*, spec: WaitAnyDetachedInput) -> WaitAnyDetachedResult:
    """Wait until the first of ``spec.targets`` resolves, then report all of them.

    Returns ``worker_exited`` as soon as any watched lease pid dies within the
    budget; ``no_live_worker`` immediately when any target has no live worker at
    the FIRST poll (it already exited or was never launched — there is already
    something actionable, so holding a seat would be dead air, and the
    degenerate all-dead set lands here too); and ``timeout`` when the budget
    elapses with every watched worker still alive. Every return carries the full
    per-target snapshot in the input's order.
    """
    from hpc_agent._kernel.lifecycle.detached import pid_alive
    from hpc_agent.state.run_record import current_homedir

    detached_dir = current_homedir() / "_detached"
    started = time.monotonic()
    have_dir = detached_dir.is_dir()

    # ── First poll: resolve every target's live lease (wait-detached's own
    # call-time resolution, applied across the set). ──────────────────────────
    watches: list[_Watch] = []
    for target in spec.targets:
        lease = _live_lease(detached_dir, target.run_id, target.block) if have_dir else None
        pid = int(lease["pid"]) if lease is not None else None
        watches.append(_Watch(target=target, lease=lease, pid=pid))

    dead_now = [w for w in watches if w.lease is None]
    if dead_now:
        # At least one target is already resolved — return AT ONCE with the full
        # snapshot rather than blocking on the siblings. This is exactly what
        # wait-detached does for a single dead target, and it keeps the fleet
        # waiter from sitting on an actionable item (or on a target that was
        # never launched) for a whole chunk.
        rows = [
            _row(
                w,
                detached_dir,
                state="no_live_worker" if w.lease is None else "still_running",
                triggered=w.lease is None,
                have_dir=have_dir,
            )
            for w in watches
        ]
        return WaitAnyDetachedResult(
            outcome="no_live_worker",
            targets=rows,
            triggered_count=len(dead_now),
            waited_sec=0.0,
        )

    # ── The wait: probe every watched pid each tick, in input order. ─────────
    while True:
        exited = False
        for watch in watches:
            if watch.pid is not None and not pid_alive(watch.pid):
                watch.exited = True
                exited = True
        if exited:
            rows = [
                _row(
                    w,
                    detached_dir,
                    state="worker_exited" if w.exited else "still_running",
                    triggered=w.exited,
                    have_dir=have_dir,
                )
                for w in watches
            ]
            return WaitAnyDetachedResult(
                outcome="worker_exited",
                targets=rows,
                triggered_count=sum(1 for w in watches if w.exited),
                waited_sec=round(time.monotonic() - started, 3),
            )
        if time.monotonic() - started >= spec.timeout_sec:
            # Every watched worker outlived the budget — no terminal to read for
            # any of them, so the snapshot is all-still_running with no payload.
            rows = [
                _row(w, detached_dir, state="still_running", triggered=False, have_dir=have_dir)
                for w in watches
            ]
            return WaitAnyDetachedResult(
                outcome="timeout",
                targets=rows,
                triggered_count=0,
                waited_sec=round(time.monotonic() - started, 3),
            )
        time.sleep(spec.poll_interval_sec)
