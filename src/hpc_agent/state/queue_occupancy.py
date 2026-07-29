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
from hpc_agent.state.queue_intake import INTAKE_STATES, read_intake_items

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "OCCUPYING_JOURNAL_STATUSES",
    "compose_campaign_id",
    "intake_item_campaign_id",
    "occupancy_detail",
    "occupied_slots",
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
          "items": [                     # ledger half
              {"item_id": str, "state": str, "run_id": str | None}, ...
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
    counting it would hold a slot no live job is using.
    """
    runs: list[dict[str, Any]] = []
    run_slots: set[str] = set()
    if campaign_id:
        for record in find_runs_by_campaign(experiment_dir, campaign_id):
            if record.status not in OCCUPYING_JOURNAL_STATUSES:
                continue
            if getattr(record, "superseded_by", ""):
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
    if campaign_id:
        for item in read_intake_items(experiment_dir):
            if item.get("state") not in INTAKE_STATES:
                continue
            if intake_item_campaign_id(item) != campaign_id:
                continue
            item_id = item.get("item_id")
            if not isinstance(item_id, str) or not item_id:
                continue
            run_id = item.get("run_id")
            run_id = run_id if isinstance(run_id, str) and run_id else None
            items.append({"item_id": item_id, "state": item.get("state"), "run_id": run_id})
            item_slots.add(slot_key(run_id=run_id, item_id=item_id))

    slots = run_slots | item_slots
    return {
        "campaign_id": campaign_id,
        "occupied": len(slots),
        "slots": sorted(slots),
        "runs": runs,
        "items": items,
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
