"""CHAIN-DISPATCH — the run queue's WAKE EDGE: a retiring run ticks the queue.

``docs/plans/run-queue-placement-2026-07-28.md`` §5 names the wake this module
implements: *"the detached watch terminals that already exist are the
capacity-freed signal — a run finishing IS the moment to re-tick the queue."*
Everything else in the queue was already event-driven or cheap-on-relaunch;
this was the last always-draining gap. ``queue-advance`` decides, ``queue-dispatch``
actuates, ``queue-status`` reports — and until now NOTHING invoked the actor when
a dispatched run RETIRED, so the next waiting ledger item sat there until a human
or a drain pass happened by.

The shape is the repo's shipped SELF-CHAINING precedent, not a new mechanism
(``docs/internals/harness-contract.md`` capability 3: "the detached-worker
machinery — S2/S3/S4 detach, campaign reconcile self-chaining, the driver
watchdog"). A campaign's reconcile chain keeps itself alive by having each tick
schedule the next; the queue's drain keeps itself alive by having each
RETIREMENT schedule one dispatch tick. No daemon, no poller, no model in the
loop — the work that just finished is the thing that pokes the queue.

Where it fires (the two driver terminal seats)
-----------------------------------------------
A queue-dispatched run has exactly two drivers that can land its retirement,
and both call :func:`chain_dispatch_on_retire` at their terminal step:

1. ``ops/campaign_run.py::campaign_run`` on its SYNCHRONOUS path — the body the
   detached child re-enters with ``detach=False``. This is the driver
   ``queue-dispatch`` itself starts (``ops/queue/dispatch.py::_start_lifecycle``
   → ``campaign_run(spec, detach=True)``), so it is the seat that owns the
   common case. The chain sits beside the relay-due marker and
   ``_record_campaign_terminal`` — the same "terminal step" bundle, in that
   order (see "Settle first" below).
2. ``_kernel/lifecycle/block_drive.py::_chain`` at its ``terminal`` return — the
   driver the ``queue-drain`` plan relays per drivable item (Phase 3). A queue
   item that parked for a greenlight is driven to terminal there, not by the
   campaign-run child, so instrumenting only seat 1 would leave the whole
   post-park drain un-woken.

The DETACHED parent of ``campaign_run`` is deliberately NOT a seat: it returns a
handle, nothing retired, and the child it spawned reaches seat 1 for real. That
asymmetry is why the chain is placed on the synchronous body rather than on the
verb.

Retirement is decided by ONE predicate
--------------------------------------
``state/queue_occupancy.run_occupies`` — the same test that retires a pool slot
for the occupancy count and for the janitor's compaction. A driver "reaching a
terminal" and a run "retiring a slot" are NOT the same fact: ``campaign-run``'s
``run_timeout`` stage is a terminal step of the ITERATION whose cluster jobs are
still live, and a ``superseded_by`` run retires without any driver terminal at
all. Asking the shipped predicate rather than pattern-matching a stage set is
what keeps this module from inventing a second notion of "settled" — the exact
divergence ``ops/queue/maintenance.py`` calls "the one way this janitor could
corrupt the queue".

``run_occupies(None)`` is ``False``, but its docstring is explicit that ``None``
means only *no record*, never *no slot*. So a missing record is treated here as
"nothing retired" and does not chain: a ``submit_failed`` iteration that never
minted a RunRecord freed no capacity.

WHY EVERY retirement chains, not only queue-originated ones
------------------------------------------------------------
The plan's §10.S3 ledger DOES carry a natural join key (``item_run_id`` — run
ids are COMPUTED at enqueue), so "was this run one of ours?" is answerable. It
is deliberately NOT asked, for two reasons that both point the same way:

* **It would cost more than it saves.** Answering it means reading and folding
  the whole intake ledger — which is precisely what ``queue-dispatch`` does on
  its first line anyway. A gate that pays the full price of the thing it is
  gating is not a gate.
* **It would be WRONG at the edges.** Cluster capacity is not partitioned by
  provenance: a hand-submitted run retiring on ``alpha`` frees an ``alpha`` slot
  a queued item is waiting for, and ``queue-advance`` counts occupancy over the
  runs, not over the ledger. Chaining only for queue-originated retirements
  would reintroduce the wedge this module exists to close, one cluster over.

What IS gated is the only thing that must be: the ledger's existence. An
experiment with no ``.hpc/queue/intake.jsonl`` never used the queue, and the
chain costs it exactly one non-creating ``stat`` and returns ``None`` — no
import of the dispatcher, no journal read, no disclosure noise on a result that
has nothing to disclose. That is the §7 relaunch-cheapness invariant applied to
the wake edge.

Settle first, then chain (fire-and-forget)
-------------------------------------------
Every call site invokes this AFTER its terminal settlement is durable — the
relay-due marker armed, the terminal recorded, the block spans' state on disk,
the driver's own result object already built. Nothing this module does can
un-settle a retirement, and :func:`chain_dispatch_on_retire` NEVER raises:
every failure (a locked ledger, a missing ``clusters.yaml``, a dispatcher
exception) is caught and returned as data on the disclosure dict, exactly the R4
posture ``ops/queue/maintenance.groom_queue_stores`` takes toward grooming. A
wake edge that could fail a run's settlement would be strictly worse than the
gap it closes.

No amplification, by construction
----------------------------------
One call → at most ONE ``queue-dispatch`` invocation, and each driver tick calls
this at most once, so a tick can never chain more dispatch ticks than it retired
runs. The recursion is bounded for a structural reason rather than by a counter:
``queue-dispatch``'s only actuation seat is ``campaign_run(spec, detach=True)``,
whose parent branch returns a handle without ever reaching the synchronous body
this module hooks. So a chained dispatch cannot re-enter the chain in-process —
it hands off to detached children, and each of THOSE chains once, later, when it
retires. That is the intended drain cadence (A retires → B starts → B retires →
C starts), one tick per event, and it terminates when the ledger runs dry
because a dispatch with nothing to place starts nothing.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from hpc_agent.state.queue_intake import intake_path_if_exists
from hpc_agent.state.queue_occupancy import run_occupies

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["chain_dispatch_on_retire"]

_log = logging.getLogger(__name__)


def _skipped(run_id: str | None, origin: str, reason: str) -> dict[str, Any]:
    """A disclosure row for a chain that ran and decided not to dispatch."""
    return {"chained": False, "run_id": run_id, "origin": origin, "reason": reason}


def chain_dispatch_on_retire(
    experiment_dir: Path,
    *,
    run_id: str | None,
    origin: str,
) -> dict[str, Any] | None:
    """Tick ``queue-dispatch`` once because *run_id* just retired. Never raises.

    *origin* names the driver seat that fired (``"campaign-run"`` /
    ``"block-drive"``) so a disclosure can be traced back to the tick that
    produced it. Returns:

    * ``None`` — this experiment has no intake ledger, so there is no queue to
      wake and nothing to disclose. The cheapest exit (one non-creating
      ``stat``) and the one every non-queue run in the tree takes.
    * ``{"chained": False, "run_id", "origin", "reason"}`` — the ledger exists
      but nothing was chained, with the reason said out loud.
    * ``{"chained": True, ..., "stage_reached", "dispatched", "refused"}`` — one
      ``queue-dispatch`` tick ran; its own outcome counts are echoed verbatim,
      never re-judged here.
    * the same dict with ``"error"`` — the tick raised and the raise was
      swallowed. The retirement it was chained from is already settled; this
      module's whole contract is that a failed wake is data.
    """
    # The cheapest possible gate, first: a repo that never enqueued anything
    # pays one stat() for the wake edge. ``_if_exists`` because a READ must not
    # scaffold the tree it is reporting on (F46) — the same rule that keeps
    # ``queue-status`` off ``intake_path``.
    if not intake_path_if_exists(experiment_dir).exists():
        return None

    rid = (run_id or "").strip()
    if not rid:
        return _skipped(None, origin, "the tick drove no run, so nothing retired.")

    from hpc_agent.state.journal import load_run
    from hpc_agent.state.run_record import journal_root_if_exists

    # Same non-creating probe ``queue-status`` uses before its join: ``load_run``
    # reaches creating accessors, and a ledger without a journal namespace has no
    # run to have retired anyway.
    if not journal_root_if_exists(experiment_dir).exists():
        return _skipped(rid, origin, "no journal namespace — no run has retired here.")

    try:
        record = load_run(experiment_dir, rid)
    except Exception as exc:  # noqa: BLE001 — a wake edge never fails its caller
        return {
            **_skipped(rid, origin, f"could not read the RunRecord for {rid!r}."),
            "error": f"{type(exc).__name__}: {exc}",
        }

    if record is None:
        # run_occupies(None) is False, but its docstring says None means "no
        # record", never "no slot" — a run that never existed retired nothing.
        return _skipped(rid, origin, f"no RunRecord for {rid!r} — nothing retired.")
    if run_occupies(record):
        return _skipped(
            rid,
            origin,
            (
                f"{rid} still occupies a pool slot (status {record.status!r}) — the "
                "driver reached a terminal step but the run did not retire."
            ),
        )

    from hpc_agent._wire.workflows.queue_dispatch import QueueDispatchSpec
    from hpc_agent.ops.queue.dispatch import queue_dispatch

    retired_as = (
        f"superseded by {record.superseded_by}"
        if getattr(record, "superseded_by", "")
        else f"status {record.status!r}"
    )
    row = {
        "chained": True,
        "run_id": rid,
        "origin": origin,
        "reason": f"{rid} retired ({retired_as}); chained one queue-dispatch tick.",
    }
    try:
        # LEDGER-WIDE, never narrowed to this run's cid. ``queue-advance`` owns
        # placement (R3) and already scopes its own decision to what capacity
        # allows; narrowing here would be this module deciding where the freed
        # capacity may be spent, which is exactly the authority it must not take.
        result = queue_dispatch(experiment_dir=experiment_dir, spec=QueueDispatchSpec())
    except Exception as exc:  # noqa: BLE001 — the retirement is already settled
        _log.warning("queue chain-dispatch after %s failed (%s)", rid, exc)
        row["error"] = f"{type(exc).__name__}: {exc}"
        row["reason"] = (
            f"{rid} retired ({retired_as}); the chained queue-dispatch tick failed. "
            "The retirement is settled and unaffected — the queue drains on the "
            "next dispatch (a later retirement, a drain pass, or a human)."
        )
        return row

    row["stage_reached"] = result.stage_reached
    row["dispatched"] = len(result.dispatched)
    row["refused"] = len(result.refused)
    row["reason"] = (
        f"{rid} retired ({retired_as}); chained one queue-dispatch tick — "
        f"{result.stage_reached} ({len(result.dispatched)} started, "
        f"{len(result.refused)} refused)."
    )
    _log.info("queue chain-dispatch after %s: %s", rid, row["reason"])
    return row
