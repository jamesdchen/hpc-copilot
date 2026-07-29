"""The ONE "occupies a pool slot" predicate — shared by queue and campaign.

``docs/plans/run-queue-placement-2026-07-28.md`` §10.S3 fixes the definition::

    occupied(cid) = journal status ∈ {in_flight, submitting}
                  ∪ intake items for cid in state {queued, placed}
    pool_room     = max(0, K − occupied(cid))

and R9 fixes where it lives: **exactly one module, importable by both
``queue-advance`` and (later) ``campaign-advance``**. A second inline copy is
the defect this module exists to prevent — three call sites already count
occupancy slightly differently (``meta/campaign/atoms/status.py`` counts
non-terminal records for a cid; ``meta/campaign/atoms/advance.py`` derives
``pool_room`` from an ``in_flight`` count alone, so a ``submitting`` orphan is
invisible to it; ``ops/campaign_refill.py`` documents in prose that an orphan
does not shrink ``pool_room``), and the queue's arrival makes that divergence
load-bearing rather than cosmetic.

The bug S3 names, concretely: an enqueued-but-undispatched item is in NEITHER
the journal nor the sidecar store, so every refill tick inside the
enqueue→dispatch window re-enqueues the same slots. Intake is not a second
status store (R1) — it contributes exactly ONE fact no existing store holds:
*this slot is committed to this cid and is not yet a run.* That is the union's
second term and its whole justification.

Why it lives in ``state/`` and not under a subject
--------------------------------------------------
``hpc_agent.state.*`` and ``hpc_agent.infra.*`` are the two roots any subject
in any role may import (``scripts/lint_subject_imports.py``). The shared
predicate must be reachable from ``ops/queue/`` AND from
``meta/campaign/atoms/``; parking it in either subject would make the other's
import a cross-subject violation, which is precisely the pressure that
produces a second inline copy. The directional rule ``state`` must not import
``ops`` is respected: this module reads ``state/index.py`` and
``state/queue_intake.py`` and nothing else.

Dedup: the slot key, not the row
--------------------------------
A placed item and the run it became are ONE slot, not two. Intake never learns
that an item was dispatched (R1 — ``dispatched`` is a projection), so a naive
union double-counts an item across the handoff window. The union is therefore
taken over SLOT KEYS: a run contributes ``run:<run_id>``, and an intake item
contributes ``run:<run_id>`` when its computed ``run_id`` is known (§10.S2:
run ids are derived at enqueue, not minted at dispatch) and ``item:<item_id>``
otherwise. An item that resolves to an already-running run collapses onto it
exactly; an unresolved item honestly holds its own slot until it resolves.

That collapse is also §10.S2's "known collision" made visible rather than
silent: two ledger rows whose resolved params and ``run_name`` agree compute
the SAME ``run_id`` and therefore the same slot key.

A slot RELEASES when the run it became retires
-----------------------------------------------
The collapse above has a corollary the first cut missed, and it is the
difference between a working pool and a wedged one. Intake has no terminal
state (D8: ``{queued, placed}`` and nothing wider) and nothing compacts it, so
a folded item lives on the ledger FOREVER. If the ledger half counted every
such item, a campaign that dispatched and COMPLETED ``K`` iterations would read
as permanently full: the journal half correctly drops the terminal records, the
ledger half keeps contributing their slot keys, and ``pool_room = K − occupied``
sticks at zero for the rest of the experiment's life. Verified before the fix:
four ``placed`` items whose runs were all ``complete`` reported ``occupied=4``
with an empty journal half.

So an item is counted only while the run it names is UNRETIRED — no record yet
(the enqueue→dispatch window, which is the whole reason the ledger half exists)
or a live one. A terminal or superseded record retires the item's slot by the
SAME two tests the journal half applies to the record itself, which is what
makes the two halves one predicate rather than two. An item with no known
``run_id`` cannot be joined to anything and holds its own slot honestly, exactly
as before.

The join is by ``run_id`` and not by campaign attribution: an item's run is
looked up in this campaign's records first and read directly from the journal
otherwise, because a RunRecord that landed with a different (or blank)
``campaign_id`` than the item is still the run that item became, and leaving it
uncounted-as-retired would reproduce the wedge for exactly the campaigns whose
ids do not compose.

What changes for the second caller
----------------------------------
``campaign-advance``'s pool arithmetic (``meta/campaign/atoms/advance.py``
``_refill``) is this module's intended second caller, and routing it here moves
the number in TWO directions on purpose:

* **up** — an enqueued-but-undispatched item now occupies. That is S3's bug
  closed: without it every refill tick inside the enqueue→dispatch window
  re-enqueues slots that are already spoken for.
* **down** — a SUPERSEDED run stops occupying. ``campaign-status.in_flight``
  counts every non-terminal record; a record the supersession organ has closed
  is holding a slot no live job is using. A run that reached a TERMINAL status
  stops occupying too, and so does the ledger item that became it (see "A slot
  RELEASES when the run it became retires" above) — without that second half
  the rewiring would trade S3's re-enqueue window for a permanent wedge.

Only the POOL arithmetic moves. The ``wait_in_flight`` / drain-before-stop legs
keep counting live journal records, because their question is "would stopping
now orphan a cluster JOB?" — a queued ledger item is not a job, and counting it
there would make a converged campaign drain forever behind an item nothing is
going to dispatch.

Which cid a ledger item counts under
-------------------------------------
Only a PLACEMENT record stamps ``campaign_id`` (``queue_intake``'s record
shapes), so keying the ledger half on the stored field alone would make R9's
``{queued, placed}`` reduce to ``{placed}`` — the union's second term would be
structurally dead and the enqueue→dispatch window S3 named would stay
uncounted, which is the whole bug this module exists to close. So a queued
item's cid is DERIVED, by the one composition rule
(:func:`compose_campaign_id`, ``docs/design/campaign-multi-cluster.md`` §2):
its campaign base plus its explicit cluster pin. Derived only when BOTH are
present — an unpinned item's cluster is ``queue-advance``'s to decide (R3/R4),
so it is committed to no particular cid yet and counting it against a guessed
one would be the placement guess R4 forbids, dressed as arithmetic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent._kernel.contract.vocabulary import TERMINAL_STATUSES, JournalStatus
from hpc_agent.state.index import find_runs_by_campaign
from hpc_agent.state.journal import load_run
from hpc_agent.state.queue_intake import INTAKE_STATES, item_run_id, read_intake_items

if TYPE_CHECKING:
    from pathlib import Path

    from hpc_agent.state.run_record import RunRecord

__all__ = [
    "OCCUPYING_JOURNAL_STATUSES",
    "compose_campaign_id",
    "intake_item_campaign_id",
    "occupancy_detail",
    "occupied_slots",
    "run_occupies",
    "slot_key",
]

#: The journal half of the union — every NON-terminal ``JournalStatus``, today
#: ``{submitting, in_flight}``. Derived from the vocabulary rather than written
#: out, so a future non-terminal status joins the predicate by construction
#: instead of by somebody remembering this literal. ``submitting`` is in by that
#: same construction and must be: it is the pre-dispatch state a submit mints
#: BEFORE the remote actuation, and an orphaned one can name a LIVE array whose
#: id-read was severed (the provenance-review F2 argument campaign-status
#: already applies).
OCCUPYING_JOURNAL_STATUSES: frozenset[str] = frozenset(
    str(status) for status in JournalStatus if status not in TERMINAL_STATUSES
)


def slot_key(*, run_id: str | None, item_id: str | None) -> str:
    """The dedup key for one occupied slot — ``run:<run_id>`` or ``item:<item_id>``.

    Prefers ``run_id``: a run and the intake item that became it are one slot
    (see the module docstring). The two namespaces are prefixed so an item id
    can never accidentally collide with a run id.
    """
    if run_id:
        return f"run:{run_id}"
    if item_id:
        return f"item:{item_id}"
    raise ValueError("slot_key requires at least one of run_id / item_id")


def run_occupies(record: RunRecord | None) -> bool:
    """Whether *record* holds a pool slot — the ONE test both halves apply.

    ``True`` for a non-terminal, non-superseded record. ``False`` for a
    terminal or superseded one, and for ``None`` — which means only "no record",
    never "no slot": an intake item with no RunRecord yet IS the enqueue→dispatch
    window the ledger half exists to count, so its caller adds the item's own
    slot rather than consulting this.

    Extracted rather than inlined twice because the journal half and the ledger
    half must retire a slot on exactly the same evidence. Two copies of "is this
    run still occupying?" is the divergence R9 forbids for the count itself,
    scaled down to one predicate — and the first cut of this module had exactly
    that asymmetry: it dropped a terminal RECORD but kept the ledger ITEM that
    became it, so a healthy campaign wedged after ``K`` completed iterations.
    """
    if record is None:
        return False
    if getattr(record, "superseded_by", ""):
        return False
    return record.status in OCCUPYING_JOURNAL_STATUSES


def compose_campaign_id(campaign_base: str | None, cluster: str | None) -> str | None:
    """``<base>_<clusterkey>`` — the ONE composition rule, or ``None``.

    ``docs/design/campaign-multi-cluster.md`` §2 names the per-cluster campaign
    id after its base and its cluster key. COMPOSED, never parsed: a base
    routinely contains underscores (``ebm_all_buckets_carc``), so splitting one
    back apart is a hazard that doc calls out by name.

    Lives here, next to the predicate that consumes it, because occupancy and
    ``queue-advance`` must agree byte-for-byte on which cid an item belongs to —
    a second inline ``f"{base}_{cluster}"`` is the same class of divergence R9
    forbids for the count itself. ``None`` when either half is missing or blank:
    an open-loop item belongs to no campaign, and an unpinned item's cluster is
    not yet decided.
    """
    if not isinstance(campaign_base, str) or not campaign_base:
        return None
    if not isinstance(cluster, str) or not cluster:
        return None
    return f"{campaign_base}_{cluster}"


def intake_item_campaign_id(item: dict[str, Any]) -> str | None:
    """The cid a folded intake *item* occupies, or ``None`` when undetermined.

    The stored ``campaign_id`` when a placement record put one there; otherwise
    the composition of the item's campaign base and its explicit cluster pin
    (see the module docstring's "Which cid a ledger item counts under"). A
    queued item with a base but no pin returns ``None`` — it is committed to a
    campaign but not yet to a cluster, so it occupies no cluster's pool slot.

    Public because ``queue-advance`` must ask the SAME question when deciding
    whether a placement it just decided newly occupies a slot or merely
    confirms one the predicate already counted.
    """
    stored = item.get("campaign_id")
    if isinstance(stored, str) and stored:
        return stored
    pin = item.get("cluster_pin")
    if not isinstance(pin, str) or not pin:
        # A queued item's bare ``cluster`` can only have come from the enqueue
        # (nothing but a placement record sets it later, and a placement also
        # sets ``campaign_id``, handled above), so it reads as a pin.
        pin = item.get("cluster")
    return compose_campaign_id(item.get("campaign_base"), pin)


def _journal_present(experiment_dir: Path) -> bool:
    """Does this experiment have a journal namespace yet — asked WITHOUT minting one.

    F46, the guard ``state/index.py`` and ``ops/queue/status.py`` both open with.
    ``load_run`` resolves through ``JournalLayout.run_record`` ->
    ``state/run_record.journal_dir``, which MKDIRS ``~/.claude/hpc/<repo_hash>/``
    and writes ``repo.json`` on access — so the join below would mint a ghost
    namespace for any experiment whose ledger carries a ``run_id`` and whose
    journal does not exist yet, from a pure read. Computed once per call, for
    the same reason ``queue-status`` computes it once: the pass this guard
    short-circuits is exactly the pass that finds nothing.
    """
    from hpc_agent.state.run_record import journal_root_if_exists

    return journal_root_if_exists(experiment_dir).exists()


def _item_run_record(
    experiment_dir: Path, run_id: str | None, known: dict[str, RunRecord], *, journal: bool
) -> RunRecord | None:
    """The RunRecord an intake item became, or ``None`` when it has not yet.

    Answers from this campaign's own records first — they are already in hand,
    and that is the case for every item whose run started under this cid. Falls
    back to a direct journal read because a run's ``campaign_id`` stamp comes
    off its submit spec, not off the ledger item: a record that landed with a
    blank or different campaign id is still the run this item became, and
    missing it would leave that item occupying a slot forever (the wedge, for
    exactly the campaigns whose ids do not compose). ``None`` when the item
    carries no run id at all — nothing to join on, so it holds its own slot.

    *journal* is :func:`_journal_present`'s answer, threaded in rather than
    re-asked: with no namespace there is nothing to find AND the read that would
    look must not happen at all (F46).
    """
    if run_id is None:
        return None
    record = known.get(run_id)
    if record is not None:
        return record
    return load_run(experiment_dir, run_id) if journal else None


def occupancy_detail(experiment_dir: Path, campaign_id: str) -> dict[str, Any]:
    """Full, DISCLOSABLE occupancy for *campaign_id* — the evidence behind the count.

    Returns::

        {
          "campaign_id": str,
          "occupied": int,               # len(slots) — the R9 number
          "slots": [str, ...],           # sorted slot keys (the union)
          "runs": [                      # journal half
              {"run_id": str, "status": str, "cluster": str}, ...
          ],
          "items": [                     # ledger half — the items that DO occupy
              {"item_id": str, "state": str, "run_id": str | None}, ...
          ],
          "retired_items": [             # counted by neither half, and why
              {..., "retired_by": str},  # the run's terminal status, or its superseder
          ],
          "shared_slots": [str, ...],    # slots BOTH halves claim (the handoff window)
        }

    Placement is a decision a human signs (§3: placement lives inside the y), so
    the count it rests on must be inspectable — a brief that says "carc at 2
    occupied" has to be able to name which two. ``shared_slots`` is the
    double-count that WOULD have happened, surfaced rather than hidden: a
    non-empty list is the normal, healthy enqueue→dispatch handoff.

    An empty *campaign_id* yields a zero report: open-loop submits belong to no
    campaign and ``find_runs_by_campaign`` matches nothing (its own contract).

    A superseded run does NOT occupy: ``superseded_by`` marks the record closed
    by the supersession organ even when its status has not yet caught up, and
    counting it would hold a slot no live job is using. ``retired_items`` is the
    ledger half of that same rule (:func:`run_occupies` applied to the run an
    item became) and is DISCLOSED rather than merely skipped: an operator asking
    "why is this campaign at 2 of 4?" must be able to see the items that were on
    the ledger and are no longer holding anything.
    """
    runs: list[dict[str, Any]] = []
    run_slots: set[str] = set()
    known: dict[str, RunRecord] = {}
    if campaign_id:
        for record in find_runs_by_campaign(experiment_dir, campaign_id):
            known[record.run_id] = record
            if not run_occupies(record):
                continue
            runs.append(
                {
                    "run_id": record.run_id,
                    "status": record.status,
                    "cluster": record.cluster,
                }
            )
            run_slots.add(slot_key(run_id=record.run_id, item_id=None))

    items: list[dict[str, Any]] = []
    item_slots: set[str] = set()
    retired: list[dict[str, Any]] = []
    if campaign_id:
        journal = _journal_present(experiment_dir)
        for item in read_intake_items(experiment_dir):
            if item.get("state") not in INTAKE_STATES:
                continue
            if intake_item_campaign_id(item) != campaign_id:
                continue
            item_id = item.get("item_id")
            if not isinstance(item_id, str) or not item_id:
                continue
            run_id = item_run_id(item)
            row: dict[str, Any] = {
                "item_id": item_id,
                "state": item.get("state"),
                "run_id": run_id,
            }
            became = _item_run_record(experiment_dir, run_id, known, journal=journal)
            if became is not None and not run_occupies(became):
                # The run this item became has retired. The item is still on the
                # ledger (intake has no terminal state and nothing compacts it),
                # so counting it would hold the slot forever — see the module
                # docstring's "A slot RELEASES when the run it became retires".
                row["retired_by"] = became.superseded_by or became.status
                retired.append(row)
                continue
            items.append(row)
            item_slots.add(slot_key(run_id=run_id, item_id=item_id))

    slots = run_slots | item_slots
    return {
        "campaign_id": campaign_id,
        "occupied": len(slots),
        "slots": sorted(slots),
        "runs": runs,
        "items": items,
        "retired_items": retired,
        "shared_slots": sorted(run_slots & item_slots),
    }


def occupied_slots(experiment_dir: Path, campaign_id: str) -> int:
    """How many pool slots *campaign_id* currently occupies (the R9 number).

    ``pool_room = max(0, K - occupied_slots(...))``. Thin wrapper over
    :func:`occupancy_detail` so the count and the evidence can never disagree —
    a caller that wants only the number must not get it from a second, cheaper,
    subtly different scan.
    """
    return int(occupancy_detail(experiment_dir, campaign_id)["occupied"])
