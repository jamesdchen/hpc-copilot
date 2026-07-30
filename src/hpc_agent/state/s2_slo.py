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

*The greenlight.* The EARLIEST decision record for the run whose ``response`` is
``"y"`` and whose ``resolved.next_block`` targets ``submit-s2`` or later
(:data:`FIRE_TARGETS`). S2 is where local intent becomes remote reality, so the
y that authorizes entering it is where the attended stretch starts. Later y's in
the same chain (the S2→S3 boundary that authorizes the main array) fall INSIDE
the measured window on purpose — they are latency the human paid, and they are
also counted by ``interventions_count``. Taking the S3 y as the start instead
would measure the machine's last mile and hide exactly the wait the design is
about.

*Array accepted.* ``RunRecord.submitted_at``. **Disclosed imprecision:** on the
``submit_and_record`` path that stamp is taken with the scheduler's job ids
already in hand — a true accept instant. On the submit-once path
(``mint_submitting_record`` → ``promote_submitting_record``) the record is minted
BEFORE dispatch, so ``submitted_at`` is the pre-dispatch instant and the measured
interval is an UNDER-estimate by the dispatch duration. Closing that is a
one-field integration seam — a dedicated accept stamp written at
``promote_submitting_record`` and whitelisted in ``run_record._UPDATABLE_FIELDS``
— owned by the submit chain, not by this reducer. :func:`compute_slo` accepts an
explicit ``accepted_at`` override so the seam, once wired, needs no change here.

*Readiness age at fire.* :func:`hpc_agent.state.readiness.ledger_age_sec` against
the greenlight instant: the age of the freshest atom that already existed then.
Exact whenever no atom was refreshed since; where one was, that kind is excluded
rather than back-dated, so the number is always an age some atom really had.
``None`` when there is no ledger, no cluster host to look one up by, or no atom
predating the fire — honest, never zero.

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
    "FIRE_TARGETS",
    "SLO_FIELDS",
    "S2Slo",
    "compute_slo",
    "greenlight_fire_record",
    "interventions_count",
    "slo_for_run",
]


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

#: The three SLO field names, in render order. Also the FIELD_KIND keys the
#: monitor-summary telemetry registry declares — one list, two consumers.
SLO_FIELDS: tuple[str, ...] = (
    "y_to_array_accepted_seconds",
    "interventions_count",
    "readiness_age_at_fire_seconds",
)


@dataclass(frozen=True)
class S2Slo:
    """The three SLO numbers for one run. Every field independently ``None``-able."""

    y_to_array_accepted_seconds: int | None = None
    interventions_count: int = 0
    readiness_age_at_fire_seconds: int | None = None

    def as_dict(self) -> dict[str, Any]:
        """The field mapping in :data:`SLO_FIELDS` order."""
        return {name: getattr(self, name) for name in SLO_FIELDS}

    @property
    def measured(self) -> bool:
        """True when anything at all was measurable (an all-null SLO is not rendered)."""
        return (
            self.y_to_array_accepted_seconds is not None
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


def greenlight_fire_record(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The EARLIEST greenlight that opened the attended window, or ``None``.

    "Earliest" is by append (chronological) order, which the decision journal
    guarantees — no timestamp sort, so a record with a malformed ``ts`` cannot
    reorder the chain. See the module docstring for why the S2-targeting y, not
    the S3 one, is the start boundary.
    """
    for record in records:
        if not isinstance(record, dict) or record.get("response") != "y":
            continue
        if _journaled_target(record.get("resolved")) in FIRE_TARGETS:
            return record
    return None


def interventions_count(records: list[dict[str, Any]]) -> int:
    """Human decision records within the submit chain for this run.

    Counts EVERY decision record whose ``block`` is a submit-chain verb — the
    nudges as well as the y's, because a nudge is a human stop too and hiding it
    would make a painful submit look cheap. Records for other workflows
    (aggregate, campaign, notebook) in the same run scope are not submit-chain
    interventions and are excluded.
    """
    chain = _submit_chain_blocks()
    return sum(
        1
        for record in records
        if isinstance(record, dict)
        and isinstance(record.get("block"), str)
        and record["block"] in chain
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
    """Reduce decision *records* + an accept stamp into the three SLO numbers.

    The pure core: hand it the run's decision records (chronological), the
    ``submitted_at``/accept stamp, and the cluster host whose readiness ledger to
    date the fire against. No I/O except the readiness-ledger read, no clock.

    :param records: the run scope's decision records, in append order.
    :param accepted_at: the array-accepted ISO-8601 stamp — today
        ``RunRecord.submitted_at``; pass a dedicated accept stamp here once the
        submit chain writes one (see the module docstring's seam).
    :param host: ssh host key for the readiness lookup; ``""`` skips it.
    """
    from hpc_agent.infra.time import parse_iso_utc_or_none

    fire = greenlight_fire_record(records)
    fire_at = parse_iso_utc_or_none((fire or {}).get("ts")) if fire else None
    accepted = parse_iso_utc_or_none(accepted_at)

    elapsed: int | None = None
    if fire_at is not None and accepted is not None:
        delta = (accepted - fire_at).total_seconds()
        # A negative interval means the accept stamp predates the y (a resumed
        # run whose record was minted on an earlier attempt, or clock skew
        # between writers). Report None rather than a negative "latency" — an
        # uninterpretable number is worse than an absent one.
        elapsed = int(delta) if delta >= 0 else None

    return S2Slo(
        y_to_array_accepted_seconds=elapsed,
        interventions_count=interventions_count(records),
        readiness_age_at_fire_seconds=(
            _readiness_age(host, fire_at=fire_at) if fire_at is not None else None
        ),
    )


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
        accepted_at=getattr(record, "submitted_at", None),
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
