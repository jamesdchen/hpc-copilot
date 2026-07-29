"""The run queue's JANITOR — the one place ledger + journal history is groomed.

``docs/plans/run-queue-placement-2026-07-28.md`` §7 states the invariant this
module exists to hold:

    A drain pass that starts, finds nothing drivable, and returns must cost
    near-zero. Any future pass-startup work that scales with ledger HISTORY
    (rather than with currently-drivable items) violates it.

§8 S12 found it already violated in two places, both measured against the
shipped code rather than assumed:

1. ``queue-status`` reads and folds the WHOLE append-only intake ledger on
   every call (``read_intake_records_counted`` → ``fold_intake_records``), so a
   pass over an experiment that has run five hundred items pays for five
   hundred even when two are drivable;
2. its ``occupancy`` leg calls ``state/queue_occupancy.occupied_slots`` →
   ``state/index.find_runs_by_campaign``, which ``load_run``s EVERY run file in
   the journal namespace — including every terminal one — once per distinct
   campaign on the page.

Both are history, not active work, and both are removed by deleting records
nothing still asks about: :func:`groom_queue_stores` compacts the intake ledger
and then prunes terminal journal records.

Where it runs, and why THAT is the authority point
--------------------------------------------------
``queue-dispatch``, at the end of a tick that actually wrote a placement. Four
constraints pick that seat and only that seat:

* **Never on a read path.** ``queue-status`` and ``queue-advance`` declare
  ``side_effects=[]`` and are routed through non-creating accessors precisely so
  a query cannot touch the store it reports on (F46). A janitor there would be
  that bug one layer up, and it would make two identical reads produce different
  files.
* **``queue-advance`` is pure by contract (R3)** — it decides and writes
  nothing. It cannot host a write.
* **``queue-run`` must stay cheap** — §1: "enqueueing spends nothing". Charging
  every arrival an O(history) sweep would be a new tax on the one verb the
  design promises is free.
* **The dispatcher is already the queue's only actor**, already holds the
  per-cid dispatch lock, already appends to the ledger, and already pays one
  ``read_intake_items`` walk. The marginal cost of grooming there is a second
  pass over a file it has just written, on the tick that made the ledger
  longer — and a tick that dispatched NOTHING is charged nothing at all, which
  is exactly the pass §7's invariant is written about.

Ordering is load-bearing, not incidental
----------------------------------------
Compaction runs FIRST, prune SECOND, and the prune is handed the ids that
SURVIVED compaction. ``queue-status`` projects ``dispatched`` / ``run_status`` /
``terminal`` by joining an item to its RunRecord, so pruning a record a live
ledger item still references would silently flip that item back to
``dispatched=false`` — the projection would start lying about a run that really
happened. Compacting first removes every item whose run retired; passing the
survivors as ``protect`` makes the "still referenced" case unreachable rather
than merely unlikely. S12's "define pruned-target semantics" is that pair of
rules.

Retirement is decided by ONE predicate, and it is NARROWER than "not occupying"
------------------------------------------------------------------------------
``state/queue_occupancy.retired_item_census`` — built from the same
``run_occupies`` test the occupancy count applies per campaign (R9), then
narrowed by that module's ``item_is_history``. Compaction therefore cannot
remove an item the pool arithmetic still counts as occupying: a second, subtly
different notion of "settled" is the one way this janitor could corrupt the
queue, so there is not one.

The relationship is CONTAINMENT, not equality, and the difference is load-bearing
(adversarial-review F1). "Stopped occupying a slot" and "is history" are
different questions: a ``failed`` run releases its slot immediately — it must, or
a campaign wedges at ``K`` after its first failure — but its ledger row is not
history, because ``queue-dispatch``'s decision table mints a FRESH attempt over a
corpse rather than adopting it. Treating the two as one deleted live intents:
a ``queued`` retry re-enqueued over a failed run, and a ``placed`` row whose
placement was appended (durable-first) before a start that then refused. Both are
work no other store records. So the janitor's set is a strict subset of the
non-occupying set, and the safety argument runs in the direction that cannot
lose data: everything this pass drops is provably unoccupied, but not everything
unoccupied is dropped. The predicate module owns the full argument.

Concurrency: the census is verified UNDER the ledger lock
----------------------------------------------------------
Deciding what retired means reading and joining the run stores, which cannot
happen inside the ledger's flock. The dispatch lock does not cover the gap
either — it is per-cid while the ledger is global — so a ``queue-dispatch`` tick
for another campaign can append a placement for an item this census has already
condemned. The census therefore carries the RECORD COUNT it witnessed per item,
and ``compact_intake_ledger`` re-counts under the lock and declines to drop
anything that moved (``maintenance.raced_items``). Without that check the rewrite
deletes a row another process is simultaneously reporting as ``placed``, or —
when the append lands just after the rewrite — strands a transition record the
fold can never open and the drop set can never reach again.

Never destroy the thing you are operating on
--------------------------------------------
``docs/internals/adding-a-primitive.md`` states the rule outright ("a
prune/cleanup step must exclude the run it is currently submitting"), and this
janitor can violate it in one specific, reachable way: adopting an item onto an
ALREADY-``complete`` run retires that item the instant the placement lands, so a
naive sweep would erase — in the same tick — the ledger record the dispatcher is
simultaneously reporting as ``placed``. ``queue-dispatch`` would say "adopted,
placed=true" about an item ``queue-status`` could no longer find. So the caller
passes the ids it touched as *exclude_item_ids* and they survive this pass; the
next tick, which is not reporting on them, may compact them.

Never raises
------------
Grooming is hygiene. A locked ledger, a read-only journal, a full disk — none of
them may turn a successful dispatch into an error envelope, because the dispatch
already happened and the items already started. Every failure is caught and
reported as data on the result (``maintenance.error``), which is the same R4
posture the rest of the queue takes toward per-item trouble.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from hpc_agent.state.queue_intake import compact_intake_ledger, read_intake_items
from hpc_agent.state.queue_occupancy import retired_item_census

if TYPE_CHECKING:
    from collections.abc import Collection
    from pathlib import Path

__all__ = ["KEEP_TERMINAL_RUNS", "groom_queue_stores"]

_log = logging.getLogger(__name__)

#: How many terminal RunRecords the journal prune keeps, newest first.
#:
#: Generous rather than tight, deliberately. ``prune_terminal_runs`` has existed
#: since the journal did and has NEVER been called from src (§8 S12 named it as
#: "wired, never called"); this is its first production caller, so the first cut
#: errs toward keeping evidence. It is still a bound — ``find_runs_by_campaign``
#: becomes O(active + 100) instead of O(all runs ever) — which is what the
#: invariant actually asks for. The queue's own join is protected explicitly
#: (see :func:`groom_queue_stores`), so this number governs only history no
#: ledger item points at.
KEEP_TERMINAL_RUNS = 100


def groom_queue_stores(
    experiment_dir: Path,
    *,
    exclude_item_ids: Collection[str] = (),
    keep_terminal_runs: int = KEEP_TERMINAL_RUNS,
) -> dict[str, Any]:
    """Compact the intake ledger, then prune unreferenced terminal runs.

    *exclude_item_ids* are the items the CALLER is currently reporting on; they
    are never compacted by this pass (see "Never destroy the thing you are
    operating on" above), and because they survive the compaction their runs are
    also protected from the prune by construction.

    Returns a disclosure dict — ``{"dropped_items", "dropped_records",
    "kept_records", "raced_items", "reaped_orphans", "pruned_runs",
    "protected_runs"}``, plus ``"error"`` when a leg failed. Nothing here is
    silent: a janitor that shrank a durable store without saying by how much
    would make every later "where did that item go?" unanswerable. ``raced_items``
    names the items a concurrent appender touched between this pass's census and
    the ledger lock — they were NOT compacted, and saying so is what keeps
    "nothing was dropped" checkable rather than assumed.

    ``{}`` is impossible — an experiment with nothing to groom reports zeros, so
    a caller can always quote the numbers.
    """
    report: dict[str, Any] = {
        "dropped_items": 0,
        "dropped_records": 0,
        "kept_records": 0,
        "raced_items": [],
        "reaped_orphans": 0,
        "pruned_runs": 0,
        "protected_runs": 0,
    }
    try:
        # ONE read behind both halves of the census: the ids to drop and the
        # record count witnessed for each. Two reads would open, between
        # themselves, the very race the counts are handed to compaction to close.
        exempt = set(exclude_item_ids)
        census = {
            item_id: count
            for item_id, count in retired_item_census(experiment_dir).items()
            if item_id not in exempt
        }
        compaction = compact_intake_ledger(
            experiment_dir, drop_item_ids=set(census), witnessed_records=census
        )
        report["dropped_items"] = int(compaction["dropped_items"])
        report["dropped_records"] = int(compaction["dropped_records"])
        report["kept_records"] = int(compaction["kept_records"])
        report["raced_items"] = list(compaction.get("raced_items") or [])
        report["reaped_orphans"] = int(compaction.get("reaped_orphans") or 0)

        # Read the ledger AFTER the rewrite: these are the runs a surviving item
        # still joins to, and they are exempt from the prune no matter how old.
        protect = {
            run_id
            for item in read_intake_items(experiment_dir)
            if isinstance(run_id := item.get("run_id"), str) and run_id
        }
        report["protected_runs"] = len(protect)

        from hpc_agent.state.index import prune_terminal_runs

        report["pruned_runs"] = prune_terminal_runs(
            experiment_dir, keep_terminal_runs, protect=protect
        )
    except Exception as exc:  # noqa: BLE001 — hygiene must never fail a dispatch
        _log.warning("queue maintenance: grooming %s failed (%s)", experiment_dir, exc)
        report["error"] = f"{type(exc).__name__}: {exc}"
    return report
