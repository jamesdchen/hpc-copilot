"""The S2 SLO reducer — pillar 6 of ``docs/design/s2-readiness.md``.

"Unmeasured devx regresses." The design names three numbers:

* ``y_to_array_accepted_seconds`` — seconds from the journaled greenlight to the
  array being accepted by the scheduler. The headline: it is what pillars 1-5
  exist to drive down.
* ``interventions_count`` — how many human decision records the submit chain
  cost for this run. Every relay-to-human is a stop; the count is the tax.
* ``readiness_age_at_fire_seconds`` — how old the standing readiness ledger was
  when S2 fired. Pillar 1's own scorecard: a fire against a fresh ledger is the
  designed path, a fire against an old one is the reactive path in disguise.

**A pure reducer, no new clocks.** Every field is computed from records that
already exist — the decision journal's ``ts`` stamps, the run journal's
``submitted_at``, and the readiness ledger's own atom stamps. This module writes
nothing, stamps nothing, and calls no clock of its own: the caller supplies the
records (or the experiment/run to read them from) and, where "now" is needed at
all, it is not — every number here is a difference between two recorded instants.

Boundary definitions (stated so they cannot drift)
--------------------------------------------------

*The attended stretch starts at a y targeting ``submit-s2``.* S2 is where local
intent becomes remote reality, so the y that authorizes entering it is the start.
Later y's inside the same attempt (the S2→S3 boundary that authorizes the main
array) fall INSIDE the measured window on purpose — they are latency the human
paid, and ``interventions_count`` counts them too. Taking the S3 y as the start
would measure the machine's last mile and hide exactly the wait this is about.

*Which y, when a run was re-driven?* A run that fails and is re-driven re-enters
S2, journaling a NEW ``submit-s2`` y each time. Measured from the FIRST one, a
run re-driven overnight reads ~40000s — a number dominated by how long the human
was asleep between attempts, which no amount of S2 engineering can move. So there
are two fields, and both ship (2026-07-30 review, F5):

* :attr:`S2Slo.y_to_array_accepted_seconds` — **last-attempt scoped**, the
  primary. Measured from the LAST ``submit-s2`` y at or before the accept stamp:
  the attempt that actually produced this array. This is the number the SLO is
  an SLO *of* — it moves when S2 gets better.
* :attr:`S2Slo.first_y_to_array_accepted_seconds` — measured from the first
  ``submit-s2`` y of the whole run. The day-scale view: how long the human waited
  from first committing to having an array, re-drives included.

Neither is "the" truth and the pair is deliberately both-reported rather than one
silently chosen. **Open question for review:** whether the day-scale field should
subtract idle time between attempts (it currently does not — an unattended gap is
indistinguishable in the journal from an attended one).

*What counts as an intervention.* Every decision record in the submit chain, plus
the mid-submit recovery boundaries in :data:`RECOVERY_INTERVENTION_BLOCKS`.
Nudges count as well as y's: a nudge is a human stop too, and hiding it would make
a painful submit look cheap. ``host-retarget`` was excluded until the 2026-07-30
review measured a real run — 10 counted where 12 humans stops happened — which is
exactly the kind of undercount that makes a scorecard flattering and useless.

*Array accepted.* ``RunRecord.accepted_at`` when present, else
``RunRecord.submitted_at``. The dedicated stamp is written by
``ops.submit.runner.promote_submitting_record`` — the one site that runs with the
scheduler's parsed job ids in hand — and is whitelisted in
``run_record._UPDATABLE_FIELDS`` so it rides the same locked write as the ids.
This closed the under-estimate the field shipped with (2026-07-30 drift-log seam
b): on the submit-once path (``mint_submitting_record`` →
``promote_submitting_record``) the record is minted BEFORE dispatch, so
``submitted_at`` is a pre-dispatch instant and every latency measured to it was
short by the dispatch duration.

The fallback is EXACT rather than degraded wherever it fires. On the
``submit_and_record`` path ``submitted_at`` is itself taken with the job ids
already parsed, so it already means "accepted" and there is nothing to correct;
records written before the stamp existed keep reading exactly as they did. The
ordering — dedicated stamp first, ``submitted_at`` second — is what makes the
field an improvement rather than a schema change: nothing has to be backfilled.

*Readiness age at fire.* :func:`hpc_agent.state.readiness.ledger_age_sec` against
the greenlight instant: the age of the freshest atom that already existed then.
**A RETROACTIVE RECONSTRUCTION, not a stamp taken at the time** — the ledger keeps
only the latest atom per identity, so an identity refreshed after the fire is
excluded rather than back-dated. The number is therefore always an age some atom
really had, but it can be OLDER than what the fire actually consulted (the atom it
saw may since have been overwritten). Every surface that shows it must say so;
the render marks it ``(reconstructed)``. Making it exact is a stamp at the S2
consult site — the pillar-1 integration seam. ``None`` when there is no ledger, no
cluster host to look one up by, or no atom predating the fire — honest, never zero.

Every field is independently ``None``-able and a missing field never poisons the
others: a run with no journaled y still reports its ``interventions_count``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

__all__ = [
    "ATTEMPT_ENTRY_TARGET",
    "FIRE_TARGETS",
    "RECOVERY_INTERVENTION_BLOCKS",
    "SLO_FIELDS",
    "S2Slo",
    "accept_stamp",
    "compute_slo",
    "first_greenlight_record",
    "greenlight_fire_record",
    "interventions_count",
    "slo_for_run",
]

#: The ``next_block`` whose greenlight ENTERS the attended stretch. A re-driven
#: run journals a fresh one per attempt, which is what makes last-attempt scoping
#: mechanical rather than a heuristic about gaps between timestamps.
ATTEMPT_ENTRY_TARGET = "submit-s2"

#: Blocks outside the submit chain whose decision records are still human stops
#: inside one submit, and therefore still interventions. Extending this set is a
#: reviewed edit, not an incidental one: every member makes the scorecard
#: larger, and a scorecard that undercounts its own cost is worse than none
#: (2026-07-30 review: ``host-retarget`` was missing and a real run read 10
#: interventions where the human had stopped 12 times).
RECOVERY_INTERVENTION_BLOCKS: frozenset[str] = frozenset({"host-retarget"})


def _fire_targets() -> frozenset[str]:
    """``submit-s2`` and every block after it in the submit chain.

    Derived from ``infra.block_chain.ORDER["submit"]`` rather than re-listed, so a
    chain change cannot silently re-point the SLO's start boundary.
    """
    from hpc_agent.infra.block_chain import ORDER

    chain = ORDER.get("submit") or []
    if "submit-s2" not in chain:  # pragma: no cover — pinned by the chain test
        return frozenset({"submit-s2"})
    return frozenset(chain[chain.index("submit-s2") :])


#: The ``resolved.next_block`` targets whose greenlight opens the attended window.
FIRE_TARGETS: frozenset[str] = _fire_targets()

#: The SLO field names, in render order. Also the FIELD_KIND keys the
#: monitor-summary telemetry registry declares — one list, two consumers.
SLO_FIELDS: tuple[str, ...] = (
    "y_to_array_accepted_seconds",
    "first_y_to_array_accepted_seconds",
    "interventions_count",
    "readiness_age_at_fire_seconds",
)


@dataclass(frozen=True)
class S2Slo:
    """The SLO numbers for one run. Every field independently ``None``-able.

    The two latency fields answer different questions and are both reported: see
    the module docstring's "Which y, when a run was re-driven?".
    """

    #: Last-attempt scoped — the attempt that produced this array. The primary.
    y_to_array_accepted_seconds: int | None = None
    #: From the run's FIRST attended y, re-drives included. The day-scale view;
    #: equal to the primary for a run that was never re-driven.
    first_y_to_array_accepted_seconds: int | None = None
    interventions_count: int = 0
    #: A retroactive reconstruction, not a stamp taken at fire time — surfaces
    #: must mark it as such (the render appends "(reconstructed)").
    readiness_age_at_fire_seconds: int | None = None

    def as_dict(self) -> dict[str, Any]:
        """The field mapping in :data:`SLO_FIELDS` order."""
        return {name: getattr(self, name) for name in SLO_FIELDS}

    @property
    def redriven(self) -> bool:
        """True when the run entered S2 more than once (the two latencies differ)."""
        return (
            self.y_to_array_accepted_seconds is not None
            and self.first_y_to_array_accepted_seconds is not None
            and self.first_y_to_array_accepted_seconds != self.y_to_array_accepted_seconds
        )

    @property
    def measured(self) -> bool:
        """True when anything at all was measurable (an all-null SLO is not rendered)."""
        return (
            self.y_to_array_accepted_seconds is not None
            or self.first_y_to_array_accepted_seconds is not None
            or self.interventions_count > 0
            or self.readiness_age_at_fire_seconds is not None
        )


def _submit_chain_blocks() -> frozenset[str]:
    """The submit workflow's block verbs (``infra.block_chain.ORDER``)."""
    from hpc_agent.infra.block_chain import ORDER

    return frozenset(ORDER.get("submit") or ())


# MIRROR: hpc_agent.ops.block_gate::_journaled_target pinned-by tests/state/test_s2_slo.py::test_journaled_target_is_in_lockstep_with_the_block_gate  # noqa: E501
def _journaled_target(resolved: Any) -> str | None:
    """The ``next_block`` a decision's ``resolved`` payload targets, if any.

    A deliberate replica of ``ops/block_gate._journaled_target``: the value is
    either a bare verb string or a ``{"verb": …}`` mapping. Replicated rather
    than imported because that symbol is package-private to ``ops`` and
    ``scripts/lint_private_cross_package_imports.py`` refuses the cross-package
    reach; the lockstep test above is what keeps the two from drifting — if the
    gate's reading of a greenlight ever diverged from this reducer's, the SLO
    would silently measure from a boundary the driver never used.

    Read-only and permissive — an unrecognized shape yields ``None``.
    """
    if not isinstance(resolved, dict):
        return None
    target = resolved.get("next_block")
    if isinstance(target, dict):
        target = target.get("verb")
    # The empty string is returned AS the empty string, never collapsed to
    # None. It makes no behavioural difference here (``"" not in
    # FIRE_TARGETS``) — which is exactly why an unpinned replica would have
    # drifted unnoticed. The annotated test above caught the collapse.
    return target if isinstance(target, str) else None


def _is_attended_greenlight(record: Any, *, targets: frozenset[str] | set[str]) -> bool:
    """True when *record* is a ``y`` whose journaled target is in *targets*."""
    return (
        isinstance(record, dict)
        and record.get("response") == "y"
        and _journaled_target(record.get("resolved")) in targets
    )


def first_greenlight_record(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The run's FIRST attended greenlight, or ``None``.

    Feeds :attr:`S2Slo.first_y_to_array_accepted_seconds` — the day-scale view
    that includes every re-drive. "First" is by append (chronological) order,
    which the decision journal guarantees; no timestamp sort, so a record with a
    malformed ``ts`` cannot reorder the chain.

    Matches the whole of :data:`FIRE_TARGETS`, not just
    :data:`ATTEMPT_ENTRY_TARGET`: a run that was resumed straight into S3 never
    journaled an S2-entry y at all, and the day-scale number should still start
    where the human first committed.
    """
    for record in records:
        if _is_attended_greenlight(record, targets=FIRE_TARGETS):
            return record
    return None


def greenlight_fire_record(
    records: list[dict[str, Any]], *, accepted_at: datetime | None = None
) -> dict[str, Any] | None:
    """The greenlight of the attempt that produced the array, or ``None``.

    The LAST y targeting :data:`ATTEMPT_ENTRY_TARGET` at or before *accepted_at*
    — each such y is a fresh entry into the attended stretch, so "the last one
    before the accept" is exactly "the attempt that produced this array". This is
    mechanical, not a heuristic about gaps between timestamps: the chain itself
    marks every re-entry.

    *accepted_at* bounds the scan so a LATER re-drive (a run re-driven again after
    this array landed) cannot be mistaken for the attempt that produced it.
    Records whose ``ts`` is unparseable are not excluded by the bound — an
    undateable record is not evidence that it came after.

    Falls back to :func:`first_greenlight_record` when nothing targeted
    ``submit-s2`` (a run resumed straight into S3), so the primary field is never
    silently null for a run that really was attended.
    """
    from hpc_agent.infra.time import parse_iso_utc_or_none

    entry_targets = {ATTEMPT_ENTRY_TARGET}
    entries = [r for r in records if _is_attended_greenlight(r, targets=entry_targets)]
    if accepted_at is not None:
        bounded = []
        for record in entries:
            ts = parse_iso_utc_or_none(record.get("ts"))
            if ts is None or ts <= accepted_at:
                bounded.append(record)
        entries = bounded
    if entries:
        return entries[-1]
    return first_greenlight_record(records)


def interventions_count(records: list[dict[str, Any]]) -> int:
    """Human decision records for this run's submit, counted honestly.

    Counts EVERY decision record whose ``block`` is a submit-chain verb OR a
    member of :data:`RECOVERY_INTERVENTION_BLOCKS` — the nudges as well as the
    y's, because a nudge is a human stop too and hiding it would make a painful
    submit look cheap. Records belonging to other workflows (aggregate, campaign,
    notebook) in the same run scope are not this submit's cost and are excluded.

    ``host-retarget`` is in the count because the 2026-07-30 review measured a
    real run: 10 counted where the human had stopped 12 times. A scorecard that
    undercounts its own cost is worse than no scorecard.
    """
    counted = _submit_chain_blocks() | RECOVERY_INTERVENTION_BLOCKS
    return sum(
        1
        for record in records
        if isinstance(record, dict)
        and isinstance(record.get("block"), str)
        and record["block"] in counted
        and isinstance(record.get("response"), str)
    )


def _readiness_age(host: str, *, fire_at: datetime) -> int | None:
    """Ledger age in whole seconds at *fire_at*, or ``None``. Never raises."""
    if not host:
        return None
    try:
        from hpc_agent.state import readiness

        age = readiness.ledger_age_sec(readiness.read_ledger(host), as_of=fire_at)
    except Exception:  # noqa: BLE001 — a reducer must never fail a render
        return None
    return None if age is None else int(age)


def compute_slo(
    records: list[dict[str, Any]],
    *,
    accepted_at: str | None,
    host: str = "",
) -> S2Slo:
    """Reduce decision *records* + an accept stamp into the SLO numbers.

    The pure core: hand it the run's decision records (chronological), the
    ``submitted_at``/accept stamp, and the cluster host whose readiness ledger to
    date the fire against. No I/O except the readiness-ledger read, no clock.

    :param records: the run scope's decision records, in append order.
    :param accepted_at: the array-accepted ISO-8601 stamp. :func:`slo_for_run`
        supplies ``RunRecord.accepted_at`` (the true scheduler-accept stamp)
        falling back to ``submitted_at``; see the module docstring.
    :param host: ssh host key for the readiness lookup; ``""`` skips it.

    The readiness age is dated against the LAST-ATTEMPT fire, not the first: it
    answers "how stale was what S2 knew when it fired", and the fire that
    produced this array is the one that consulted the ledger.
    """
    from hpc_agent.infra.time import parse_iso_utc_or_none

    accepted = parse_iso_utc_or_none(accepted_at)

    def _elapsed(fire: dict[str, Any] | None) -> tuple[int | None, datetime | None]:
        fire_at = parse_iso_utc_or_none((fire or {}).get("ts")) if fire else None
        if fire_at is None or accepted is None:
            return None, fire_at
        delta = (accepted - fire_at).total_seconds()
        # A negative interval means the accept stamp predates the y (a resumed
        # run whose record was minted on an earlier attempt, or clock skew
        # between writers). Report None rather than a negative "latency" — an
        # uninterpretable number is worse than an absent one.
        return (int(delta) if delta >= 0 else None), fire_at

    last_elapsed, last_fire_at = _elapsed(greenlight_fire_record(records, accepted_at=accepted))
    first_elapsed, _ = _elapsed(first_greenlight_record(records))

    return S2Slo(
        y_to_array_accepted_seconds=last_elapsed,
        first_y_to_array_accepted_seconds=first_elapsed,
        interventions_count=interventions_count(records),
        readiness_age_at_fire_seconds=(
            _readiness_age(host, fire_at=last_fire_at) if last_fire_at is not None else None
        ),
    )


def accept_stamp(record: Any) -> str | None:
    """The array-accepted instant for a run *record* — the ONE definition.

    ``accepted_at`` (the true scheduler-accept stamp
    ``promote_submitting_record`` writes with the job ids in hand) when present,
    else ``submitted_at``. Named and exported rather than inlined at the one call
    site because "which stamp means accepted" is precisely the thing that drifted
    — the field shipped measuring to a pre-dispatch instant on the submit-once
    path — and a second reader picking differently would reintroduce the same
    under-estimate under a different name.

    An empty / whitespace ``accepted_at`` falls back too: a blank stamp is not a
    measurement, and preferring it would make the number vanish rather than
    degrade gracefully to the value this reducer used before the field existed.
    """
    accepted = getattr(record, "accepted_at", None)
    if isinstance(accepted, str) and accepted.strip():
        return accepted
    submitted = getattr(record, "submitted_at", None)
    return submitted if isinstance(submitted, str) and submitted.strip() else None


def slo_for_run(experiment_dir: Path, run_id: str, record: Any) -> S2Slo:
    """:func:`compute_slo` for a run, reading the journals it already has.

    *record* is the run's :class:`~hpc_agent.state.run_record.RunRecord` (the
    caller already holds it; re-loading it here would double the read on the
    monitor's hot path). Total fail-open: an unreadable decision journal or an
    unresolvable cluster yields the empty SLO, never an exception into a render.
    """
    try:
        from hpc_agent.state.decision_journal import read_decisions

        records = read_decisions(experiment_dir, "run", run_id)
    except Exception:  # noqa: BLE001 — a reducer must never fail a render
        records = []
    return compute_slo(
        records,
        accepted_at=accept_stamp(record),
        host=_host_for_cluster(getattr(record, "cluster", "") or ""),
    )


def _host_for_cluster(cluster: str) -> str:
    """The ssh host for a ``clusters.yaml`` key, or ``""``. Never raises."""
    if not cluster:
        return ""
    try:
        from hpc_agent.infra.clusters import load_clusters_config

        clusters = load_clusters_config()
    except Exception:  # noqa: BLE001 — a broken config must not fail a render
        return ""
    cfg = clusters.get(cluster) if isinstance(clusters, dict) else None
    return str(cfg.get("host") or "").strip() if isinstance(cfg, dict) else ""
