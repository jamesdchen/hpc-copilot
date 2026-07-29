"""``queue-advance`` — the placement AUTHORITY. Pure, deterministic, disclosed.

Phase 1 of ``docs/plans/run-queue-placement-2026-07-28.md`` (§2, §3, §6). The
authority/actor split copied from ``campaign-advance`` / ``campaign-refill``:
this verb READS the intake ledger plus ``clusters.yaml`` plus the one shared
occupancy predicate and returns a DECISION — *this item, that cluster, and the
line the human's ``y`` is taken against*. ``queue-dispatch`` (Phase 2) is the
only thing that acts on it.

**R3 — pure.** No file is written, no watermark moved, no cache minted, no
cluster contacted. The empty ``side_effects`` list on the primitive is that
promise mechanized, and the ledger read is routed through
``queue_intake.intake_path_if_exists`` so a decision against a virgin
experiment does not scaffold the tree it is reporting on (F46). Purity is not
hygiene here: §3 puts the placement INSIDE the human's ``y``, so a decision
that could not be recomputed byte-for-byte from the same inputs would make the
``y`` unfalsifiable.

**R6 / §7 R3 — the scheduler IS the capacity queue.** Slurm/SGE already run
the authoritative resource queue (fairshare, priorities, backfill,
walltime-aware placement) and their state is the one thing we cannot predict
from outside. So this verb does NOT probe a scheduler, does NOT model queue
depth, and does NOT infer headroom. Two queues in series, each doing only what
it can. What remains OURS, and is the whole of what is computed below:

* **cross-cluster choice** — each scheduler sees only its own queue, so
  nothing but us can compare two clusters;
* **courtesy caps** — lab-etiquette / anti-throttle policy (the
  connection-storm lineage), enforced by us because centers penalize
  queue-flooding accounts. Phase 1 ships NO courtesy cap and therefore never
  emits ``courtesy_cap_reached``: the only per-cluster cap in the config is
  ``constraints.max_concurrent_jobs``, and S11 confirmed it is
  per-submission-plan wave grouping (``compute_submission_plan`` reads only
  ``(constraints, workload)`` — no journal, no other runs), so citing it as a
  cross-run cap would invent a second meaning for a field that already has
  one. A real account-level pending cap needs its own config key;
* **hard-constraint mismatch** — the item's typed asks against the cluster's
  declared ceilings, so an item is never placed somewhere its job would be
  refused.

**R5 — an explicit pin always wins.** A pinned item is evaluated against its
pin and NOTHING else. If the pin fails a hard constraint the item is HELD with
that reason; it is never quietly re-placed, because re-placing it would be the
tool overruling the operator on a decision the operator already made.

**R4 — every outcome carries a disclosed reason.** No item is dropped, no
cluster is guessed. A placement states the rule that fired (pin / sole
survivor / least-loaded / alphabetical tie-break) and carries the per-cluster
verdicts behind it; a hold states what the item is waiting on, with a closed
``reason_code`` so a brief can COUNT by cause ("3 items waiting on gpu")
instead of parsing prose. Items past ``max_placements`` are held as
``batch_limit_reached`` — evaluated anyway, so a bounded call cannot report a
constraint failure as a caller-imposed bound or vice versa.

**R9 — one occupancy predicate.** Load comes from
``state/queue_occupancy.occupied_slots`` and from nowhere else. Inlining a
second count here is the exact defect that module exists to prevent, and it
would let ``queue-advance`` and ``campaign-advance`` place against different
totals.

The policy is a SHIPPED deterministic default (§3), not a heuristic: filter by
hard constraints, then least-loaded, then stable alphabetical tie-break. Every
step is a total order over local data, so the same ledger and the same
``clusters.yaml`` always produce the same decision.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any, Literal

from pydantic import TypeAdapter, ValidationError

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire._shared import (
    CampaignId,
    QueueResourceAsk,
    RunIdStrict,
    Scheduler,
    SshTarget,
)
from hpc_agent._wire.queries.queue_advance import (
    QueueAdvanceResult,
    QueueAdvanceSpec,
    QueueClusterVerdict,
    QueueHoldback,
    QueuePlacement,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.infra.time import parse_iso_utc_or_none, utcnow_iso
from hpc_agent.state.queue_intake import STATE_QUEUED, read_intake_items
from hpc_agent.state.queue_occupancy import (
    compose_campaign_id,
    intake_item_campaign_id,
    occupied_slots,
)

__all__ = ["queue_advance"]

_PRIMITIVE = "queue-advance"

# Wire aliases as validators. A cluster key, a campaign base and a config's
# ``user@host`` are all UNTRUSTED strings here (yaml a human edits, a ledger
# line a forward-compatible writer wrote), and the result models enforce these
# patterns. Validating through the alias rather than re-writing its regex keeps
# this verb honest about what it may emit: a value that would not survive the
# schema is reported as absent, never coerced.
_CAMPAIGN_ID: TypeAdapter[str] = TypeAdapter(CampaignId)
_RUN_ID: TypeAdapter[str] = TypeAdapter(RunIdStrict)
_SCHEDULER: TypeAdapter[str] = TypeAdapter(Scheduler)
_SSH_TARGET: TypeAdapter[str] = TypeAdapter(SshTarget)


def _text(value: Any) -> str | None:
    """A non-empty ``str`` or ``None``.

    Folded intake items carry whatever JSON the writer put there. Everything
    read off one goes through this rather than ``str(...)``, which would turn a
    stray ``null`` into the literal ``"None"`` and poison a join key or a
    cluster comparison.
    """
    return value if isinstance(value, str) and value else None


def _wire(adapter: TypeAdapter[str], raw: Any) -> str | None:
    """*raw* if it satisfies the wire alias, else ``None`` — never a coercion."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return adapter.validate_python(raw)
    except ValidationError:
        return None


def _load_clusters() -> tuple[dict[str, Any], str]:
    """``(clusters, error)`` — the live ``clusters.yaml``, or ``({}, why)``.

    An unreadable or non-mapping config is DATA (``no_clusters_configured``),
    not an exception: R4 makes "nothing could be evaluated" a disclosed hold on
    every queued item rather than a failure of the read, and the queue's own
    wire vocabulary already fixed that ruling ("the config is empty or
    unreadable"). The import is lazy so registry import — every CLI turn — does
    not pay for the yaml parse.

    Entries whose top-level key is not a non-empty STRING are dropped here. YAML
    1.1 turns a bare ``1234:`` / ``on:`` / ``2024:`` into an int or a bool, and
    every cluster lookup in the package — ``clusters.yaml[name]``,
    ``queue-run``'s pin check, ``resolve_ssh_target`` — keys on a string, so a
    placement decided on such an entry would name a cluster nothing downstream
    could resolve. Dropping it means the item is HELD (with the configured keys
    listed) rather than placed somewhere undispatchable; the alternative,
    stringifying the key, would have this one verb accept a cluster the rest of
    the system cannot see.
    """
    from hpc_agent.infra.clusters import load_clusters_config

    try:
        raw = load_clusters_config()
    except Exception as exc:  # noqa: BLE001 — any load failure is one disclosed hold
        return {}, f"clusters.yaml failed to load ({exc})"
    if not isinstance(raw, dict):
        return (
            {},
            f"clusters.yaml did not parse to a mapping of cluster keys (got {type(raw).__name__})",
        )
    if not raw:
        return {}, "clusters.yaml declares no clusters"
    clusters = {key: value for key, value in raw.items() if isinstance(key, str) and key}
    if not clusters:
        dropped = ", ".join(repr(key) for key in list(raw)[:5])
        return {}, (
            f"clusters.yaml declares no cluster whose key is a non-empty string "
            f"(got {dropped}); a non-string yaml key cannot be looked up by any "
            "cluster consumer"
        )
    return clusters, ""


def _asks(item: dict[str, Any]) -> QueueResourceAsk | None:
    """The item's typed resource asks, or ``None`` when they cannot be read.

    ``None`` is NOT "no asks": an unreadable ``resources`` blob means the hard
    constraints cannot be evaluated honestly, so the item is held as
    ``item_unresolved`` rather than placed as though it had asked for nothing.
    Silently treating garbage as an empty ask is exactly how an item that needs
    a GPU lands on a cluster that has none.
    """
    raw = item.get("resources")
    if raw is None:
        return QueueResourceAsk()
    if not isinstance(raw, dict):
        return None
    try:
        return QueueResourceAsk.model_validate(raw)
    except ValidationError:
        return None


def _unresolved_reason(item: dict[str, Any], asks: QueueResourceAsk | None) -> str | None:
    """Why placement cannot evaluate this item at all, or ``None`` when it can.

    Two ways an item is unplaceable in principle: its resource asks do not
    parse (above), or it names neither an inline spec nor a ``spec_ref``, so
    there is nothing for a dispatcher to resolve. ``queue-run`` refuses the
    second at the boundary, so an item reaching here without one came from a
    hand-edited or foreign ledger line — which is precisely the case that must
    surface as a stated hold instead of a silent skip (R4).
    """
    if asks is None:
        return "the item's `resources` block does not parse as a typed resource ask"
    if item.get("spec") is None and _text(item.get("spec_ref")) is None:
        return "the item names neither an inline `spec` nor a `spec_ref`, so nothing can resolve it"
    for key in ("cluster_pin", "cluster"):
        raw = item.get(key)
        # A pin the reader cannot use is NOT an unpinned item. Demoting it would
        # silently convert an operator's own choice into policy's — the exact
        # substitution R5 forbids, and the one ``queue-run`` refuses loudly at
        # the boundary ("guessing which was meant would put an undisclosed
        # placement decision on the ledger"). ``None`` is the honest absence
        # ``queue-run`` writes for an unpinned item and is not a malformed pin.
        if raw is not None and _text(raw) is None:
            return (
                f"the item carries a `{key}` that is not a usable cluster key "
                f"({raw!r}); a pin always wins over policy (R5), so it is never "
                "demoted to 'unpinned' and placement will not choose for it"
            )
    return None


def _pin(item: dict[str, Any]) -> str | None:
    """The explicit cluster pin (R5), or ``None`` when the item is unpinned.

    ``queue-run`` stores the pin as ``cluster_pin`` precisely so a placement
    record's ``cluster`` cannot be mistaken for one. A queued item's bare
    ``cluster`` is accepted as a pin too — only a placement transition sets
    that key, and this verb only ever looks at queued items — so the read
    cannot go blind on the store's choice of spelling.

    A present-but-unusable pin never reaches here: :func:`_unresolved_reason`
    holds the item first, so ``None`` from this function means "no pin was
    written", never "a pin was written and discarded".
    """
    pinned = _text(item.get("cluster_pin"))
    return pinned if pinned is not None else _text(item.get("cluster"))


def _compose_cid(campaign_base: str | None, cluster: str) -> str | None:
    """``<base>_<clusterkey>`` (campaign-multi-cluster.md §2), or ``None``.

    Composed by ``state/queue_occupancy.compose_campaign_id`` — the one rule,
    shared so this verb and the occupancy predicate cannot disagree about which
    cid an item belongs to (R9's argument applied to the key, not just the
    count). ``None`` when the item is open-loop (no base) or when the
    composition would not satisfy the ``CampaignId`` alias — a cluster key that
    cannot form a legal campaign id is reported, not coerced.
    """
    composed = compose_campaign_id(campaign_base, cluster)
    return _wire(_CAMPAIGN_ID, composed) if composed is not None else None


def _walltime_clause(cfg: Any, entry: dict[str, Any]) -> str:
    """Name WHICH walltime ceiling was compared (S11).

    ``clusters.yaml`` carries two that disagree by 8-16x: the top-level
    ``max_walltime_sec`` (documented "cluster's hard walltime ceiling", read by
    ``get_max_walltime_sec``) and ``constraints.max_walltime`` (read by the
    throughput planner as the per-array ceiling, and defaulted to ``24:00:00``
    even for a cluster that declares no constraints block at all). Placement
    compares the HARD one — the question is "will the scheduler accept this
    job", not "how should one submission be waved" — and says so out loud,
    quoting the other so a surprised human can see both numbers.

    It also says whether the number it compared appears in the operator's file
    at all: ``ClusterConfig.max_walltime_sec`` defaults, so an entry that
    declares no ceiling still holds items against 86400. Calling that "the
    cluster's ceiling" sends the operator to grep a file the number is not in,
    with no way to reconcile or fix the hold — the same absent-declaration case
    the cost-gate leg below already discloses.
    """
    constraints = entry.get("constraints")
    declared = constraints.get("max_walltime") if isinstance(constraints, dict) else None
    other = f", not constraints.max_walltime {declared!r}" if declared else ""
    source = (
        "the top-level hard ceiling"
        if "max_walltime_sec" in entry
        else "the framework default — this entry declares no max_walltime_sec"
    )
    return f"max_walltime_sec {cfg.max_walltime_sec} ({source}{other})"


def _evaluate(
    cluster: str,
    entry: Any,
    asks: QueueResourceAsk,
    *,
    campaign_base: str | None,
    occupied: int,
) -> tuple[QueueClusterVerdict, Any]:
    """One cluster's verdict for one item — TOTAL: it returns, it never raises.

    R4's "an unusable config is DATA, not an exception" is enforced here, at the
    per-cluster grain, because that is the grain of the data: one bad entry in
    ``clusters.yaml`` must cost that entry its eligibility and nothing else. The
    unguarded version was not total in three ways that all reproduce —
    ``ClusterConfig.model_validate`` raises :class:`errors.SpecInvalid` (an
    ``HpcError``, which is NOT a ``ValueError``, so pydantic re-raises it
    untouched) for an unknown ``scheduler`` family; ``parse_constraints``
    returns an unvalidated dataclass, so a quoted number in
    ``constraints.max_estimated_core_hours`` makes the comparison a
    ``TypeError``; and either escapes past the candidate loop and kills the
    whole pass, so EVERY queued item in the call loses its decision with no
    hold and no reason — the precise inversion of R4.

    Checks run in a FIXED order and the first failure wins, so the reason a
    human reads is deterministic rather than dependent on dict iteration. Every
    comparison names the config field it read and the value it found: a verdict
    that says only "does not match" is a verdict nobody can act on.

    Nothing here consults a scheduler (R6). Every input is a declared value in
    ``clusters.yaml`` or a typed ask on the item.
    """
    try:
        return _evaluate_entry(cluster, entry, asks, campaign_base=campaign_base, occupied=occupied)
    except Exception as exc:  # noqa: BLE001 — R4: one bad entry is one ineligible verdict
        return (
            QueueClusterVerdict(
                cluster=cluster,
                eligible=False,
                reason=(
                    f"clusters.yaml entry could not be evaluated "
                    f"({type(exc).__name__}: {exc}); placement will not guess at it"
                ),
                occupied=occupied,
            ),
            None,
        )


def _evaluate_entry(
    cluster: str,
    entry: Any,
    asks: QueueResourceAsk,
    *,
    campaign_base: str | None,
    occupied: int,
) -> tuple[QueueClusterVerdict, Any]:
    """The checks themselves. Free to raise — :func:`_evaluate` owns totality."""
    from hpc_agent.infra.clusters import ClusterConfig
    from hpc_agent.infra.constraints import parse_constraints

    def verdict(*, eligible: bool, reason: str) -> tuple[QueueClusterVerdict, Any]:
        return (
            QueueClusterVerdict(
                cluster=cluster, eligible=eligible, reason=reason, occupied=occupied
            ),
            None,
        )

    if not isinstance(entry, dict):
        return verdict(
            eligible=False,
            reason=(
                f"clusters.yaml entry is not a mapping (got {type(entry).__name__}); "
                "placement will not guess at it"
            ),
        )
    try:
        cfg = ClusterConfig.model_validate(entry)
    except ValidationError as exc:
        first = str(exc).splitlines()[0]
        return verdict(eligible=False, reason=f"clusters.yaml entry failed validation: {first}")

    # Composing the campaign id is a hard constraint when the item HAS a base:
    # a cluster whose key cannot form a legal `<base>_<key>` campaign id cannot
    # host a campaign item, and inventing a different id would break the exact-
    # match lookup every campaign consumer does.
    if campaign_base and _compose_cid(campaign_base, cluster) is None:
        return verdict(
            eligible=False,
            reason=(
                f"cluster key {cluster!r} cannot compose a legal campaign id with "
                f"base {campaign_base!r}"
            ),
        )

    checks: list[str] = []

    if asks.gpu:
        declares_gpu = bool(cfg.gpu_types) or bool(cfg.gpu_queues) or bool(cfg.gpu_constraint)
        if not declares_gpu:
            return verdict(
                eligible=False,
                reason=(
                    "item asks gpu=true; cluster declares no gpu_types, gpu_queues "
                    "or gpu_constraint"
                ),
            )
        checks.append("gpu=true and the cluster declares GPU resources")

    if asks.gpu_type is not None:
        available = [str(g) for g in cfg.gpu_types]
        wanted = asks.gpu_type.casefold()
        if not any(g.casefold() == wanted for g in available):
            return verdict(
                eligible=False,
                reason=(
                    f"item asks gpu_type {asks.gpu_type!r}; cluster gpu_types = {available!r} "
                    "(gpu_types is documented informational — placement promotes it to a hard "
                    "filter and discloses that rather than placing an item where its job "
                    "cannot run)"
                ),
            )
        checks.append(f"gpu_type {asks.gpu_type!r} is in gpu_types {available!r}")

    if asks.walltime_sec is not None:
        ceiling = _walltime_clause(cfg, entry)
        if asks.walltime_sec > cfg.max_walltime_sec:
            return verdict(
                eligible=False,
                reason=f"item asks walltime_sec {asks.walltime_sec} > {ceiling}",
            )
        checks.append(f"walltime_sec {asks.walltime_sec} <= {ceiling}")

    if asks.est_core_hours is not None:
        cap = parse_constraints(entry.get("constraints") or {}).max_estimated_core_hours
        if cap is not None and (isinstance(cap, bool) or not isinstance(cap, (int, float))):
            # ClusterConstraints is a plain dataclass: it stores whatever the
            # yaml held. A quoted number ("500") is an ordinary hand edit and
            # would otherwise make the comparison below a TypeError. Named as a
            # config defect rather than swallowed — an unevaluable cost gate is
            # not an absent one, and placing against it would put the item on a
            # cluster whose own gate may refuse the submission.
            return verdict(
                eligible=False,
                reason=(
                    f"cluster declares constraints.max_estimated_core_hours {cap!r} "
                    f"({type(cap).__name__}), which is not a number; the item's "
                    f"est_core_hours {asks.est_core_hours} cannot be compared against it"
                ),
            )
        if cap is not None:
            if asks.est_core_hours > cap:
                return verdict(
                    eligible=False,
                    reason=(
                        f"item asks est_core_hours {asks.est_core_hours} > "
                        f"constraints.max_estimated_core_hours {cap} — this cluster's own "
                        "declared cost gate would refuse the submission"
                    ),
                )
            checks.append(
                f"est_core_hours {asks.est_core_hours} <= "
                f"constraints.max_estimated_core_hours {cap}"
            )
        else:
            checks.append(
                f"est_core_hours {asks.est_core_hours} not compared: cluster declares no "
                "constraints.max_estimated_core_hours"
            )

    if asks.cores is not None:
        # Stated, not enforced: clusters.yaml declares no per-cluster core
        # ceiling, and inventing one would be a config key this tool made up.
        checks.append(
            f"cores {asks.cores} not compared: clusters.yaml declares no per-cluster core ceiling"
        )

    body = "; ".join(checks) if checks else "the item states no resource asks"
    return (
        QueueClusterVerdict(
            cluster=cluster,
            eligible=True,
            reason=f"satisfies every hard constraint ({body})",
            occupied=occupied,
        ),
        cfg,
    )


def _placement(
    item_id: str,
    item: dict[str, Any],
    cluster: str,
    cfg: Any,
    *,
    campaign_base: str | None,
    reason: str,
    pinned: bool,
    considered: list[QueueClusterVerdict],
) -> QueuePlacement:
    """Assemble one decided placement — every disclosed field, none inferred.

    ``ssh_target`` is resolved from config at DECISION time and is disclosure
    only: a run's recorded target is stale-by-design and re-resolved at use
    (run12 finding 23), so nothing downstream may consume this as the live
    target. ``scheduler`` is validated against the live backend registry, and
    an unregistered family is reported as absent rather than emitted — the
    field is there to say what the run can promise (S11: SGE cannot enforce the
    canary success-gate), and a value the registry does not know promises
    nothing.
    """
    return QueuePlacement(
        item_id=item_id,
        run_id=_wire(_RUN_ID, item.get("run_id")),
        cluster=cluster,
        campaign_id=_compose_cid(campaign_base, cluster),
        scheduler=_wire(_SCHEDULER, getattr(cfg, "scheduler", None)),
        ssh_target=_wire(_SSH_TARGET, getattr(cfg, "ssh_target", None)),
        pinned=pinned,
        reason=reason,
        considered=considered,
    )


def _choose_reason(
    chosen: QueueClusterVerdict,
    eligible: list[QueueClusterVerdict],
    rejected: int,
    *,
    load: dict[str, int],
    campaign_base: str | None,
) -> str:
    """The line the ``y`` is taken against — the rule that fired, not a summary.

    §3's worked example is "next: rv_sweep item 4 -> hoffman2 (carc at 2/2,
    item needs gpu)": the human must be able to check the choice, so the string
    names the deciding factor AND the runner-up it beat. Which of the three
    rules fired is stated explicitly, because "least-loaded" and "alphabetical
    tie-break on equal load" are different claims about how much the tool
    actually knew.
    """
    eliminated = (
        f"{rejected} candidate cluster(s) eliminated by hard constraints"
        if rejected
        else "no candidate was eliminated by hard constraints"
    )
    if len(eligible) == 1:
        return (
            f"{chosen.cluster} is the only cluster satisfying this item's hard "
            f"constraints ({eliminated}); {chosen.reason}"
        )
    runner_up = eligible[1]
    if not campaign_base:
        return (
            f"{chosen.cluster} chosen by alphabetical tie-break over {runner_up.cluster} — the "
            "item carries no campaign base, so cross-cluster load is not comparable through the "
            f"shared occupancy predicate (every candidate reads 0); {eliminated}; {chosen.reason}"
        )
    chosen_load = load[chosen.cluster]
    other_load = load[runner_up.cluster]
    if chosen_load == other_load:
        return (
            f"{chosen.cluster} chosen by alphabetical tie-break over {runner_up.cluster} at equal "
            f"load ({chosen_load} occupied pool slot(s) each); {eliminated}; {chosen.reason}"
        )
    return (
        f"{chosen.cluster} is the least-loaded eligible cluster at {chosen_load} occupied pool "
        f"slot(s) vs {runner_up.cluster} at {other_load}; {eliminated}; {chosen.reason}"
    )


def _brief(
    *,
    computed_at: str,
    decision: str,
    queued_total: int,
    placements: list[QueuePlacement],
    held_counts: dict[str, int],
) -> str:
    """The deterministic disclosure the session relays VERBATIM into the ``y``.

    Code-computed from the fields above and never authored: §3 puts the
    placement inside the human's ``y``, so the placement has to be in front of
    them in the same breath as the question. Sorted and pluralization-free
    where it can be, so two runs over the same state produce the same bytes.
    """
    if decision == "empty":
        return f"Run queue: nothing queued (computed {computed_at}). Nothing to place."
    lines = [
        f"Run queue: {queued_total} queued, {len(placements)} placement(s) decided, "
        f"{sum(held_counts.values())} held (computed {computed_at})."
    ]
    lines += [f"next: {p.item_id} -> {p.cluster} — {p.reason}" for p in placements]
    if held_counts:
        counts = ", ".join(f"{held_counts[code]} {code}" for code in sorted(held_counts))
        lines.append(f"held: {counts}.")
    return "\n".join(lines)


@primitive(
    name=_PRIMITIVE,
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key=None,
    cli=CliShape(
        help=(
            "The run queue's placement AUTHORITY: reads queued intake items, "
            "clusters.yaml and the one shared occupancy predicate, and returns "
            "a DECISION — which item goes to which cluster, under which "
            "'<base>_<clusterkey>' campaign id, and the disclosed reason the "
            "human's y is taken against. Policy is a shipped deterministic "
            "default: an explicit cluster pin always wins; otherwise filter "
            "candidates by hard constraints derived from the item's typed "
            "resource asks (gpu / gpu_type / walltime / est core-hours) against "
            "the cluster's declared ceilings, then least-loaded by occupied "
            "pool slots, then stable alphabetical tie-break. It does NOT probe "
            "or model scheduler capacity - the scheduler is the capacity queue; "
            "ours is cross-cluster choice, courtesy caps and hard-constraint "
            "mismatch. Every held item is reported with a closed reason_code and "
            "a human-readable reason (never dropped, never guessed), with the "
            "per-cluster verdicts behind it. Writes NOTHING: reads only, no SSH, "
            "no journal, no cache - the actor consumes the decision."
        ),
        spec_arg=True,
        # All-optional spec (the net-triage precedent): a bare
        # `hpc-agent queue-advance` decides placement over the whole queue —
        # {} is a valid, meaningful request, so the spec file is optional.
        spec_required=False,
        experiment_dir_arg=True,
        spec_model=QueueAdvanceSpec,
        schema_ref=SchemaRef(input="queue_advance"),
    ),
    agent_facing=True,
)
def queue_advance(
    *, experiment_dir: Path, spec: QueueAdvanceSpec | None = None
) -> QueueAdvanceResult:
    """Decide where the queued items go, disclose why, and write nothing.

    One pass over the in-scope queued items in ARRIVAL order — the queue's own
    fairness rule, and the only ordering that is stable under a re-read. Every
    item is evaluated, including items past ``max_placements``: a bounded call
    must not report "the caller limited the batch" for an item that would have
    been held for a constraint anyway, and it must not hide a placeable item
    behind a bound without saying which cluster it would have taken.

    Placements decided EARLIER in the same call provisionally occupy their
    cluster's slot for the rest of the pass, so a batch of three does not stack
    on one cluster because none of them has been dispatched yet. The increment
    is skipped when the shared predicate ALREADY counts the item under that cid
    — a pinned item's cid is fixed at enqueue, so placing it there confirms a
    slot rather than taking a new one, and incrementing would count it twice.
    The provisional increment is disclosed in the reason and is deliberately
    kept out of the per-cluster ``occupied`` verdict field, which stays exactly
    the shared predicate's number (R9).

    ``spec.now`` overrides the evaluation instant (the ``doctor`` /
    ``attention-queue`` precedent) and sets ``computed_at``; it is never a knob
    for reshaping the decision. Raises :class:`errors.SpecInvalid` when it is
    not ISO-8601 UTC.
    """
    spec = spec or QueueAdvanceSpec()  # spec_required=False: bare CLI call → whole-queue decision
    now = (spec.now or "").strip() or utcnow_iso()
    if parse_iso_utc_or_none(now) is None:
        raise errors.SpecInvalid(f"queue-advance: now override {spec.now!r} is not ISO-8601 UTC")

    exp = Path(experiment_dir)
    items = [item for item in read_intake_items(exp) if item.get("state") == STATE_QUEUED]
    if spec.campaign_base is not None:
        items = [item for item in items if _text(item.get("campaign_base")) == spec.campaign_base]
    queued_total = len(items)

    clusters, config_error = _load_clusters()
    # ``_load_clusters`` guarantees every key is a non-empty string, so this is a
    # total order and ``clusters[key]`` below cannot miss.
    candidates = sorted(clusters)
    if spec.clusters is not None:
        wanted = set(spec.clusters)
        candidates = [key for key in candidates if key in wanted]

    # One occupancy read per campaign id per call, memoized: the count is a
    # directory scan and the same cid recurs across items. `provisional` holds
    # what THIS call has already decided (see the docstring).
    occupancy: dict[str, int] = {}
    provisional: dict[str, int] = {}

    def occupied_for(cid: str | None) -> int:
        if not cid:
            return 0
        if cid not in occupancy:
            occupancy[cid] = occupied_slots(exp, cid)
        return occupancy[cid]

    placements: list[QueuePlacement] = []
    held: list[QueueHoldback] = []

    for item in items:
        item_id = _text(item.get("item_id"))
        if item_id is None:
            # Unreachable through the fold (it keys on item_id) but not through
            # a future writer; a nameless item cannot be reported per-item, so
            # it is counted rather than silently skipped.
            held.append(
                QueueHoldback(
                    item_id="unknown",
                    reason_code="item_unresolved",
                    reason="a folded ledger item carries no item_id, so no decision can name it",
                    detail={},
                )
            )
            continue

        campaign_base = _text(item.get("campaign_base"))
        # The cid the SHARED predicate already counts this item under (a pinned
        # item's cid is determined at enqueue). Placing onto that same cid does
        # not newly occupy anything, so `provisional` must not double it — see
        # the two increment sites below.
        item_cid = intake_item_campaign_id(item)
        asks = _asks(item)
        unresolved = _unresolved_reason(item, asks)
        if asks is None or unresolved is not None:
            held.append(
                QueueHoldback(
                    item_id=item_id,
                    reason_code="item_unresolved",
                    reason=f"placement cannot evaluate this item: {unresolved}",
                    detail={"resources": item.get("resources"), "spec_ref": item.get("spec_ref")},
                )
            )
            continue

        if not clusters:
            held.append(
                QueueHoldback(
                    item_id=item_id,
                    reason_code="no_clusters_configured",
                    reason=(
                        f"no cluster could be evaluated: {config_error}. The item stays queued — "
                        "placement never guesses a cluster (R4)."
                    ),
                    detail={"config_error": config_error},
                )
            )
            continue

        pin = _pin(item)
        at_bound = len(placements) >= spec.max_placements

        if pin is not None:
            # R5: the pin is evaluated against ITSELF and nothing else. The
            # candidate narrowing in `spec.clusters` narrows policy's choices;
            # it does not get to overrule an operator's.
            if pin not in clusters:
                close = difflib.get_close_matches(pin, sorted(clusters), n=3, cutoff=0.6)
                held.append(
                    QueueHoldback(
                        item_id=item_id,
                        reason_code="cluster_pin_unknown",
                        reason=(
                            f"the item pins cluster {pin!r}, which the active clusters.yaml does "
                            "not define. An explicit pin always wins over policy (R5), so nothing "
                            "will re-choose for this item: it stays queued until the pin names a "
                            "configured cluster."
                            + (
                                f" Did you mean {', '.join(repr(c) for c in close)}?"
                                if close
                                else ""
                            )
                        ),
                        detail={"pin": pin, "suggestions": close, "configured": sorted(clusters)},
                    )
                )
                continue
            cid = _compose_cid(campaign_base, pin)
            verdict, cfg = _evaluate(
                pin,
                clusters[pin],
                asks,
                campaign_base=campaign_base,
                occupied=occupied_for(cid),
            )
            if not verdict.eligible:
                held.append(
                    QueueHoldback(
                        item_id=item_id,
                        reason_code="no_cluster_matches_constraints",
                        reason=(
                            f"the item pins cluster {pin!r} and that cluster does not qualify: "
                            f"{verdict.reason}. The pin is NOT silently overridden (R5) — the item "
                            "stays queued until the pin or the asks change."
                        ),
                        considered=[verdict],
                        detail={
                            "pin": pin,
                            "pinned": True,
                            "asks": asks.model_dump(mode="json"),
                        },
                    )
                )
                continue
            if at_bound:
                held.append(
                    QueueHoldback(
                        item_id=item_id,
                        reason_code="batch_limit_reached",
                        reason=(
                            f"this call's max_placements={spec.max_placements} was reached first; "
                            f"the item qualifies for its pinned cluster {pin!r} and is "
                            "placeable on "
                            "the next call. Nothing is wrong with the item."
                        ),
                        considered=[verdict],
                        detail={"max_placements": spec.max_placements, "would_place_on": pin},
                    )
                )
                continue
            reason = (
                f"explicit cluster pin (R5): the item pins {pin!r} and a pin always wins over "
                f"placement policy; the pinned cluster {verdict.reason}"
            )
            placements.append(
                _placement(
                    item_id,
                    item,
                    pin,
                    cfg,
                    campaign_base=campaign_base,
                    reason=reason,
                    pinned=True,
                    considered=[verdict],
                )
            )
            if cid and cid != item_cid:
                provisional[cid] = provisional.get(cid, 0) + 1
            continue

        if not candidates:
            held.append(
                QueueHoldback(
                    item_id=item_id,
                    reason_code="no_cluster_matches_constraints",
                    reason=(
                        "the candidate cluster set is empty: this call's `clusters` restriction "
                        f"{sorted(spec.clusters or [])} matches none of the configured clusters "
                        f"{sorted(clusters)}. The item stays queued."
                    ),
                    detail={
                        "requested_clusters": sorted(spec.clusters or []),
                        "configured": sorted(clusters),
                    },
                )
            )
            continue

        verdicts: list[QueueClusterVerdict] = []
        configs: dict[str, Any] = {}
        load: dict[str, int] = {}
        carried = 0
        for key in candidates:
            cid = _compose_cid(campaign_base, key)
            base_load = occupied_for(cid)
            verdict, cfg = _evaluate(
                key, clusters[key], asks, campaign_base=campaign_base, occupied=base_load
            )
            verdicts.append(verdict)
            provisionally = provisional.get(cid, 0) if cid else 0
            carried += provisionally
            load[key] = base_load + provisionally
            if verdict.eligible:
                configs[key] = cfg

        eligible = [v for v in verdicts if v.eligible]
        rejected = len(verdicts) - len(eligible)
        if not eligible:
            held.append(
                QueueHoldback(
                    item_id=item_id,
                    reason_code="no_cluster_matches_constraints",
                    reason=(
                        f"none of the {len(verdicts)} candidate cluster(s) satisfies this item's "
                        "hard constraints; each verdict says which configured value it failed. The "
                        "item stays queued — it is never placed on a cluster whose declared limits "
                        "would refuse it (R4)."
                    ),
                    considered=verdicts,
                    detail={"asks": asks.model_dump(mode="json")},
                )
            )
            continue

        # The shipped default, in order: least-loaded, then stable alphabetical.
        # `candidates` is already sorted, so the sort is a total order and the
        # decision is reproducible from the same ledger + config.
        eligible.sort(key=lambda v: (load[v.cluster], v.cluster))
        chosen = eligible[0]
        if at_bound:
            held.append(
                QueueHoldback(
                    item_id=item_id,
                    reason_code="batch_limit_reached",
                    reason=(
                        f"this call's max_placements={spec.max_placements} was reached first; the "
                        f"item qualifies and would be placed on {chosen.cluster!r}. Nothing is "
                        "wrong with the item."
                    ),
                    considered=verdicts,
                    detail={
                        "max_placements": spec.max_placements,
                        "would_place_on": chosen.cluster,
                    },
                )
            )
            continue

        cid = _compose_cid(campaign_base, chosen.cluster)
        reason = _choose_reason(chosen, eligible, rejected, load=load, campaign_base=campaign_base)
        if carried:
            # Disclosed because the arithmetic in `reason` would otherwise not
            # reconcile with the published `occupancy` map: the difference is
            # exactly what THIS call has already decided but not written (R3).
            reason += f" (load includes {carried} placement(s) decided earlier in this call)"
        placements.append(
            _placement(
                item_id,
                item,
                chosen.cluster,
                configs[chosen.cluster],
                campaign_base=campaign_base,
                reason=reason,
                pinned=False,
                considered=verdicts,
            )
        )
        if cid and cid != item_cid:
            provisional[cid] = provisional.get(cid, 0) + 1

    held_counts: dict[str, int] = {}
    for holdback in held:
        held_counts[holdback.reason_code] = held_counts.get(holdback.reason_code, 0) + 1

    decision: Literal["place", "hold", "empty"]
    if placements:
        decision = "place"
    elif queued_total:
        decision = "hold"
    else:
        decision = "empty"

    return QueueAdvanceResult(
        computed_at=now,
        decision=decision,
        placements=placements,
        held=held,
        held_counts=held_counts,
        queued_total=queued_total,
        considered_clusters=candidates,
        occupancy=occupancy,
        brief=_brief(
            computed_at=now,
            decision=decision,
            queued_total=queued_total,
            placements=placements,
            held_counts=held_counts,
        ),
    )
