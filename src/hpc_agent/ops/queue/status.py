"""``queue-status`` — intake items ⋈ projections over the stores that already exist.

The verb R1 exists to make possible
(``docs/plans/run-queue-placement-2026-07-28.md`` §7 R1, §8 S10)::

    queue-status = fold(intake ledger) ⋈ RunRecords ⋈ pending-decision markers
                                       ⋈ committed greenlights

The intake ledger stores arrival facts and ``{queued, placed}`` and NOTHING
else. Every other fact this verb reports — ``dispatched``, ``run_status``,
``in_flight``, ``terminal``, ``parked``, ``greenlight_committed`` — is
RECOMPUTED here on every read from stores that are already durable. A ledger
that COPIED park state would drift from the journal the first time a ``y``
landed; a ledger that projects it cannot. There is no cache, no digest file,
and no write of any kind on this path (the ``attention-queue`` / ``doctor``
posture): the read is also non-creating, routed through
``queue_intake.intake_path_if_exists`` rather than the ``RepoLayout.hpc``
accessor that mkdirs — a query must never scaffold the tree it reports on
(F46).

**R7 is the point of this verb (§8 S12).** The greenlight question routes
through ``state/decision_journal.is_committed_greenlight_for_boundary`` with
the same argument shape ``ops/attention_queue.collect_greenlight_and_parked``
uses — the boundary-scoped predicate the Stop guard and ``doctor`` also key
on, never the unscoped ``is_latest_committed_greenlight`` (under which a
consumed ``y`` stays latest forever, minting a false
``greenlight-unadvanced`` on every scan after a re-park). Two "what needs me"
surfaces that disagree are worse than one surface, and this is the exact seam
where they would. If this module ever grows its own greenlight scan, that is
the bug S12 names.

**Bounded on purpose.** The relaunch-cheapness invariant (§7) says a drain
pass that starts, finds nothing drivable and returns must cost near-zero, and
an unbounded projection over ledger HISTORY is precisely the cost it forbids.
So: the spec's filters bound the join (only filter-matching items are joined
to the run stores), ``include_settled=False`` drops items whose run already
reached a terminal status, and ``limit`` clips the page while
``total_items`` / ``truncated`` keep the clip visible. Nothing here is
silent — a dropped ledger line, a hidden settled item, a clipped page, a
``run_id`` two items both claim (§10.S2) and a park ``attention-queue``'s
``in_flight``-only enumeration cannot see all surface as code-computed
``notes``.

Hold-back reasons for items still ``queued`` are NOT invented here.
``queue-advance`` is the placement authority and its hold-backs are its
output (R3/R4: pure, disclosed, unstored); this verb reports that the item is
still queued, and the ``placement_reason`` recorded on the durable placement
record once it is placed.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from hpc_agent import errors
from hpc_agent._kernel.contract.vocabulary import TERMINAL_STATUSES, JournalStatus
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire._shared import QueueResourceAsk
from hpc_agent._wire.queries.queue_status import (
    QueueStatusItem,
    QueueStatusResult,
    QueueStatusSpec,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.infra.time import parse_iso_utc_or_none, utcnow_iso
from hpc_agent.state.decision_journal import is_committed_greenlight_for_boundary
from hpc_agent.state.journal import load_run, read_pending_decision
from hpc_agent.state.queue_intake import (
    STATE_PLACED,
    STATE_QUEUED,
    fold_intake_records,
    intake_path_if_exists,
    read_intake_records_counted,
)
from hpc_agent.state.queue_occupancy import occupied_slots

if TYPE_CHECKING:
    from datetime import datetime

__all__ = ["queue_status"]

#: Terminal journal statuses as plain strings — ``RunRecord.status`` is a str,
#: and ``TERMINAL_STATUSES`` holds :class:`JournalStatus` members. Derived, not
#: written out, so a future terminal status joins by construction.
_TERMINAL_STATUS_NAMES: frozenset[str] = frozenset(str(status) for status in TERMINAL_STATUSES)

_IN_FLIGHT: str = str(JournalStatus.IN_FLIGHT)

#: The count keys always present in ``QueueStatusResult.counts``. Zero-filled so
#: a brief can quote ``counts["parked"]`` without a membership test and so an
#: empty queue and a missing key are never confusable.
_COUNT_KEYS: tuple[str, ...] = (
    STATE_QUEUED,
    STATE_PLACED,
    "dispatched",
    "in_flight",
    "parked",
    "greenlight_unadvanced",
    "terminal",
)


def _text(value: Any) -> str | None:
    """A non-empty ``str`` or ``None`` — the ledger is tolerant, so nothing is trusted.

    Folded intake items can carry any JSON the writer put there (a hand-edited
    line, a forward-compatible key from a newer writer). Every string field read
    off an item goes through this rather than ``str(...)``, which would turn a
    stray ``null`` into the literal ``"None"`` and quietly poison a join key.
    """
    return value if isinstance(value, str) and value else None


def _resource_ask(raw: Any) -> QueueResourceAsk:
    """The item's typed resource asks, or an empty ask when the record is unusable.

    Echoed so a hold-back reason quoted from ``queue-advance`` ("needs gpu") is
    checkable against what was actually enqueued. Degrades to the empty ask
    rather than failing the whole read: one malformed item must not wedge a
    "what needs me" path.
    """
    if not isinstance(raw, dict):
        return QueueResourceAsk()
    try:
        return QueueResourceAsk.model_validate(raw)
    except ValidationError:
        return QueueResourceAsk()


def _age_sec(enqueued_at: str | None, now_dt: datetime) -> int | None:
    """Seconds from *enqueued_at* to the read's single instant; ``None`` if unparseable.

    §10.S3 accepts a real cost: a campaign item's trial params are chosen at
    ENQUEUE, so a long-held item reflects the strategy's knowledge then, not
    now. Age is how a human sees that the item has gone stale. Clamped at zero —
    a clock skew must read as "just arrived", never as a negative age.
    """
    stamped = parse_iso_utc_or_none(enqueued_at)
    if stamped is None:
        return None
    return max(0, int((now_dt - stamped).total_seconds()))


def _collisions(items: list[dict[str, Any]]) -> dict[str, list[str]]:
    """``run_id`` -> every ``item_id`` on the ledger that resolves to it.

    §10.S2's known collision, computed over the WHOLE ledger (not the filtered
    page): two items with identical resolved params and ``run_name`` compute the
    same run id. For a hand-enqueued pair that is arguably correct — it IS the
    same experiment — and for anything else it is a duplicate, so the resolution
    requires ``queue-status`` to SAY so rather than silently collapse two rows.
    A filter-scoped computation would hide the collision from exactly the reads
    that narrow to one of the two items.
    """
    by_run: dict[str, list[str]] = {}
    for item in items:
        run_id = _text(item.get("run_id"))
        item_id = _text(item.get("item_id"))
        if run_id is None or item_id is None:
            continue
        by_run.setdefault(run_id, []).append(item_id)
    return {run_id: ids for run_id, ids in by_run.items() if len(ids) > 1}


def _journal_present(experiment_dir: Path) -> bool:
    """Does this experiment have a journal namespace yet — asked WITHOUT minting one.

    F46, the same guard every reader in ``state/index.py`` opens with. The
    ledger side of this verb was routed through ``intake_path_if_exists``, but
    the JOIN side resolves through ``JournalLayout.run_record`` ->
    ``state/run_record.journal_dir``, which mkdirs ``~/.claude/hpc/<repo_hash>/``
    and writes ``repo.json`` on access. So a ``queue-status`` over an item that
    carries a ``run_id`` minted a ghost namespace — from a verb declaring
    ``side_effects=[]`` — and those stray ``*/repo.json`` are exactly what the
    fleet-wide walks then pick up. Computed ONCE per call rather than per item:
    the relaunch-cheapness invariant (§7) is about a pass that finds nothing,
    and that is precisely the pass this guard short-circuits.

    ``state/index.py`` writes the same guard as
    ``_current_homedir().exists() and journal_root_if_exists(...).exists()``;
    the first conjunct is a cheap short-circuit inside that package, not an
    independent condition — the namespace is a CHILD of the home dir, so it
    cannot exist while the home dir does not. Only the public probe is used
    here, since a leading-underscore symbol is package-private
    (``scripts/lint_private_cross_package_imports.py``).
    """
    from hpc_agent.state.run_record import journal_root_if_exists

    return journal_root_if_exists(experiment_dir).exists()


def _project(experiment_dir: Path, run_id: str | None, *, journal: bool) -> dict[str, Any]:
    """The R1 projection for one item: run facts recomputed, never read from intake.

    Keyed by the item's COMPUTED ``run_id`` (§10.S2 — run ids are derived at
    enqueue, not minted at dispatch), which is what makes the join possible
    while the item is still on the ledger. ``dispatched`` is "a RunRecord
    exists": S10's resolution in one predicate, because the submit-once
    ``submitting`` record already IS the handoff fact and no write spans intake
    and the journal atomically.

    The park + greenlight legs copy
    ``ops/attention_queue.collect_greenlight_and_parked`` verbatim, including
    its three load-bearing defensive details (the ``isinstance`` guards on
    ``resume_cursor`` and ``block``, and taking ``awaiting_since`` off the
    marker), so the two surfaces cannot answer the same question differently
    (R7). The greenlight is asked ONLY of a parked run: the predicate is
    boundary-SCOPED and an unparked run has no boundary to ask about.

    *journal* is :func:`_journal_present`'s answer, threaded in rather than
    re-probed per item: false means the experiment has no journal namespace, so
    every join target is absent by construction AND the creating accessors
    behind ``load_run`` / ``read_pending_decision`` must not be reached at all.
    """
    projection: dict[str, Any] = {
        "dispatched": False,
        "run_status": None,
        "in_flight": False,
        "terminal": False,
        "parked": False,
        "park_block": None,
        "awaiting_since": None,
        "greenlight_committed": False,
        "greenlight_unadvanced": False,
    }
    if run_id is None or not journal:
        return projection
    record = load_run(experiment_dir, run_id)
    if record is None:
        return projection
    status = _text(record.status)
    projection["dispatched"] = True
    projection["run_status"] = status
    projection["in_flight"] = status == _IN_FLIGHT
    projection["terminal"] = status in _TERMINAL_STATUS_NAMES

    marker = read_pending_decision(run_id, experiment_dir=experiment_dir) or {}
    if not marker:
        return projection
    cursor = marker.get("resume_cursor") or {}
    greenlit = is_committed_greenlight_for_boundary(
        experiment_dir,
        "run",
        run_id,
        next_verb=cursor.get("next_verb") if isinstance(cursor, dict) else None,
        awaiting_since=_text(marker.get("awaiting_since")),
        # F13: the parked block, so a same-boundary retraction supersedes an
        # earlier ``y`` while an unrelated later record does not stall it.
        block=marker.get("block") if isinstance(marker.get("block"), str) else None,
    )
    projection["parked"] = True
    projection["park_block"] = _text(marker.get("block"))
    projection["awaiting_since"] = _text(marker.get("awaiting_since"))
    projection["greenlight_committed"] = bool(greenlit)
    projection["greenlight_unadvanced"] = bool(greenlit)
    return projection


def _matches(item: dict[str, Any], spec: QueueStatusSpec) -> bool:
    """Ledger-side filters only — never a filter over a projected fact.

    ``state`` filters the STORED state; "show me the parked items" is a filter
    over a recomputed fact and offering it would teach callers to treat a
    projection as storage, which is the R1 error. The cluster filter matches a
    PIN or a chosen cluster, because "what is queued for carc" must answer with
    both the items headed there and the items already placed there.
    """
    if spec.state is not None and item.get("state") != spec.state:
        return False
    if spec.campaign_base is not None and _text(item.get("campaign_base")) != spec.campaign_base:
        return False
    return not (spec.cluster is not None and spec.cluster not in _cluster_facets(item))


def _cluster_facets(item: dict[str, Any]) -> set[str]:
    """The cluster keys an item can be matched on: its pin and its placement.

    An unplaced item's ``cluster`` can only have come from the enqueue (nothing
    but a placement record sets it later), so it is read as a pin; a placed
    item's ``cluster`` is the choice ``queue-advance`` disclosed. Both spellings
    of the pin (``cluster_pin`` and a bare ``cluster`` on a queued item) are
    accepted so the filter cannot go blind on the store's choice of key.
    """
    facets = {_text(item.get("cluster_pin")), _text(item.get("cluster"))}
    return {facet for facet in facets if facet is not None}


def _pin(item: dict[str, Any]) -> str | None:
    """The explicit cluster pin (R5), under either spelling; ``None`` when unpinned."""
    pinned = _text(item.get("cluster_pin"))
    if pinned is not None:
        return pinned
    if item.get("state") == STATE_QUEUED:
        return _text(item.get("cluster"))
    return None


def _placement_reason(item: dict[str, Any]) -> str | None:
    """The disclosed WHY carried on the placement record (R4/§3).

    Every placed item travels with the reason its cluster was chosen so the
    brief that quoted it and the ledger that justified it cannot drift. The
    store writes the key as ``reason``; ``placement_reason`` is accepted as the
    explicit spelling.
    """
    explicit = _text(item.get("placement_reason"))
    if explicit is not None:
        return explicit
    if item.get("state") == STATE_PLACED:
        return _text(item.get("reason"))
    return None


def _build_item(
    item: dict[str, Any],
    projection: dict[str, Any],
    *,
    collisions: dict[str, list[str]],
    now_dt: datetime,
) -> QueueStatusItem | None:
    """One wire item, or ``None`` when the folded record cannot satisfy the shape.

    A ``None`` is COUNTED and disclosed by the caller, never dropped quietly: a
    tolerant reader must not wedge the "what needs me" path, and it must not
    hide that it dropped something either.
    """
    item_id = _text(item.get("item_id"))
    run_id = _text(item.get("run_id"))
    siblings = [other for other in collisions.get(run_id or "", []) if other != item_id]
    enqueued_at = _text(item.get("enqueued_at"))
    try:
        return QueueStatusItem(
            item_id=item_id or "",
            state=item.get("state"),  # type: ignore[arg-type]
            enqueued_at=enqueued_at or "",
            updated_at=_text(item.get("updated_at")),
            age_sec=_age_sec(enqueued_at, now_dt),
            run_name=_text(item.get("run_name")),
            run_id=run_id,
            campaign_base=_text(item.get("campaign_base")),
            campaign_id=_text(item.get("campaign_id")),
            cluster_pin=_pin(item),
            cluster=_text(item.get("cluster")) if item.get("state") == STATE_PLACED else None,
            placement_reason=_placement_reason(item),
            resources=_resource_ask(item.get("resources")),
            spec_ref=_text(item.get("spec_ref")),
            collides_with=sorted(siblings),
            **projection,
        )
    except ValidationError:
        return None


@primitive(
    name="queue-status",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key=None,
    cli=CliShape(
        help=(
            "The run queue's bounded projection: intake items joined to the run "
            "stores. Folds .hpc/queue/intake.jsonl into current items (states "
            "'queued'/'placed' — the only two the ledger stores) and RECOMPUTES "
            "every other fact per read — whether the item became a run, is in "
            "flight, is parked awaiting a human, carries a committed-but-"
            "unadvanced greenlight, or is terminal — from RunRecords, "
            "pending-decision markers and the decision journal, routing the "
            "greenlight question through the same boundary-scoped predicate "
            "attention-queue uses so the two 'what needs me' surfaces cannot "
            "disagree. Reports per-campaign occupied pool slots from the one "
            "shared occupancy predicate. Filterable by state / campaign base / "
            "cluster, bounded by limit with truncated + total_items, and "
            "disclosing every drop (skipped ledger lines, hidden settled items, "
            "run_id collisions) in notes. Read-only, no SSH, non-creating, no "
            "cache: recomputed on every read."
        ),
        spec_arg=True,
        # All-optional spec (the net-triage precedent): a bare
        # `hpc-agent queue-status` is the whole-ledger read — {} is a valid,
        # meaningful request, so demanding a literal {} file would be ceremony.
        spec_required=False,
        experiment_dir_arg=True,
        spec_model=QueueStatusSpec,
        schema_ref=SchemaRef(input="queue_status"),
    ),
    agent_facing=True,
)
def queue_status(*, experiment_dir: Path, spec: QueueStatusSpec | None = None) -> QueueStatusResult:
    """Fold the intake ledger, join it to the run stores, and return one bounded digest.

    Order of work, and why it is that order: filter FIRST (the spec's filters
    are the bound on how much of the ledger gets joined), project the matches,
    count over every match, then hide settled items and clip the page. Counting
    before hiding is what lets ``counts["terminal"]`` stay truthful while
    ``items`` stays short.

    ``spec.now`` overrides the evaluation instant (the ``doctor`` /
    ``attention-queue`` precedent): it sets the single ``computed_at`` stamp and
    the instant ages are measured against. It is never a knob for reshaping
    ages.

    Writes nothing — not a file, not a watermark, not a cache. Raises
    :class:`errors.SpecInvalid` if ``spec.now`` is not ISO-8601 UTC.
    """
    spec = spec or QueueStatusSpec()  # spec_required=False: bare CLI call → whole-ledger read
    now = (spec.now or "").strip() or utcnow_iso()
    now_dt = parse_iso_utc_or_none(now)
    if now_dt is None:
        raise errors.SpecInvalid(f"queue-status: now override {spec.now!r} is not ISO-8601 UTC")

    exp = Path(experiment_dir)
    path = intake_path_if_exists(exp)
    records, torn = read_intake_records_counted(exp)
    folded = fold_intake_records(records)
    # Two disjoint halves of "the ledger held a line this digest does not show":
    # *torn* is what the READER dropped (not JSON, or JSON that is not an
    # object) and *unfolded* is what the FOLD could not place (a transition
    # naming an unknown item, a smuggled state outside {queued, placed}), which
    # is the difference between records read and records folded in. Summed, they
    # are the whole of what `skipped_records` promises.
    unfolded = torn + max(
        0, len(records) - sum(int(item.get("record_count") or 1) for item in folded)
    )

    collisions = _collisions(folded)

    journal = _journal_present(exp)
    matched: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for item in folded:
        if not _matches(item, spec):
            continue
        matched.append((item, _project(exp, _text(item.get("run_id")), journal=journal)))

    counts = dict.fromkeys(_COUNT_KEYS, 0)
    for item, projection in matched:
        state = item.get("state")
        if isinstance(state, str) and state in counts:
            counts[state] += 1
        for key in ("dispatched", "in_flight", "parked", "greenlight_unadvanced", "terminal"):
            if projection[key]:
                counts[key] += 1

    visible = [pair for pair in matched if spec.include_settled or not pair[1]["terminal"]]
    hidden_settled = len(matched) - len(visible)
    total_items = len(visible)
    truncated = total_items > spec.limit

    items: list[QueueStatusItem] = []
    unrenderable = 0
    for item, projection in visible[: spec.limit]:
        model = _build_item(item, projection, collisions=collisions, now_dt=now_dt)
        if model is None:
            unrenderable += 1
            continue
        items.append(model)

    # One occupancy read per DISTINCT campaign present on the matched items —
    # the R9 predicate, never a second inline count. Placement rests on this
    # number, so it is inspectable from the same read that shows the items.
    campaign_ids = {_text(item.get("campaign_id")) for item, _ in matched}
    campaign_ids.discard(None)
    occupancy = {cid: occupied_slots(exp, cid) for cid in sorted(campaign_ids) if cid is not None}

    return QueueStatusResult(
        computed_at=now,
        path=str(path),
        items=items,
        counts=counts,
        occupancy=occupancy,
        total_items=total_items,
        truncated=truncated,
        skipped_records=unfolded + unrenderable,
        notes=_notes(
            matched,
            path=path,
            counts=counts,
            collisions=collisions,
            unfolded=unfolded,
            unrenderable=unrenderable,
            hidden_settled=hidden_settled,
            truncated=truncated,
            returned=len(items),
            total_items=total_items,
            limit=spec.limit,
        ),
    )


def _notes(
    matched: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    path: Path,
    counts: dict[str, int],
    collisions: dict[str, list[str]],
    unfolded: int,
    unrenderable: int,
    hidden_settled: int,
    truncated: bool,
    returned: int,
    total_items: int,
    limit: int,
) -> list[str]:
    """Code-computed disclosure lines — each restates a fact already in the result.

    Never authored prose and never a judgement: a note exists so that something
    the digest did (dropped a line, hid a settled item, clipped a page) or
    something it saw (a shared ``run_id``, a park the fleet digest cannot
    enumerate) is legible without diffing two reads. Deterministic order, so two
    identical reads produce identical bytes.
    """
    notes: list[str] = []
    if unfolded:
        notes.append(
            f"{unfolded} ledger line(s) skipped (unparseable or not an object, a "
            f"transition naming an unknown item, or a state outside "
            f"{{queued, placed}}); inspect {path}"
        )
    if unrenderable:
        notes.append(
            f"{unrenderable} folded item(s) omitted from items: the record does not satisfy "
            "the wire shape (hand-edited or written by an incompatible writer)"
        )
    if hidden_settled:
        notes.append(
            f"{hidden_settled} settled item(s) hidden (include_settled=false); they are "
            "still counted in counts"
        )
    if truncated:
        notes.append(f"limit={limit} clipped {total_items} matching item(s) to {returned} returned")
    if counts[STATE_QUEUED]:
        notes.append(
            f"{counts[STATE_QUEUED]} item(s) still queued; queue-advance discloses the "
            "hold-back reason for each — queue-status never invents one"
        )
    for run_id in sorted(collisions):
        ids = ", ".join(sorted(collisions[run_id]))
        notes.append(
            f"run_id {run_id} is claimed by {len(collisions[run_id])} items ({ids}): "
            "identical resolved params and run_name compute one run id (§10.S2), said "
            "here rather than silently collapsed"
        )
    for item, projection in matched:
        if projection["parked"] and projection["run_status"] != _IN_FLIGHT:
            notes.append(
                f"item {_text(item.get('item_id'))} is parked on run "
                f"{_text(item.get('run_id'))} whose status is "
                f"{projection['run_status']!r}: attention-queue enumerates parks over "
                "in_flight runs only, so that park is invisible to the fleet digest"
            )
    for item, projection in matched:
        if item.get("state") == STATE_PLACED and not projection["dispatched"]:
            notes.append(
                f"placed item {_text(item.get('item_id'))} has no run record for run_id "
                f"{_text(item.get('run_id'))}: not yet dispatched, or the record was "
                "pruned — indistinguishable from here"
            )
    return notes
