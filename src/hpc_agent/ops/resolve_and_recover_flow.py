"""Resolve-and-recover composite — the #240 flow-wiring of the #234 resolver.

The deterministic resolver (:func:`hpc_agent.ops.recover.resolve.resolve`) and
the escalation funnel primitives are already built and tested; #234 is the
*resolution logic*. This module is its **buildable wiring** (#240): the one
auto-fire composite that turns a per-cluster :class:`Resolution` into either an
automatic resubmit (``decided_by="code"``) or a parked escalation
(``decided_by="judgement"``), modelled closely on the blessed
:mod:`hpc_agent.ops.auto_resume_flow` template.

The structural parallel to ``auto_resume_flow`` is deliberate — same layer,
same shape:

* a ``fetch_failures`` query (injected for testability),
* a pure decide gate — **but the general** :func:`resolve` keyed on the widened
  ``(error_class, temporal_context, resource_spec)`` evidence vector, in place
  of auto-resume's preempted-only ``decide_auto_resume_from_ids``,
* the :func:`hpc_agent.ops.recover_flow.resubmit_flow` action,
* escalate = a no-op surfaced as escalation-as-data (#231/#234), plus a *park*
  side-effect (``mark_pending_verdict``) so the held run drops out of the
  campaign loop's not-done set while everything else keeps progressing.

Policy (the #240 architectural decisions, implemented verbatim):

* ``decided_by="code"`` → **translate** the resolver's suggested-fix
  (``Resolution.action``) into concrete ``resubmit_flow`` overrides
  (:func:`_concrete_overrides`: a ``factor`` scaled against the sidecar's
  current ``mem_mb`` / ``walltime_sec``), then auto-resubmit, bumping the run's
  ``auto_recover_count`` against ``max_auto_recovers``. A fix ``resubmit_flow``
  cannot enact — a parallelism/width fix (which changes a *task kwarg*, not a
  scheduler flag), a ``factor`` with no current resource to scale, or a
  non-integer task id — is **surfaced as a ``decided_by="code"`` escalation**
  (the deterministic recommendation, for manual action) rather than
  resubmitted-identical, which would only burn the cap re-running the failing
  config.
* ``decided_by="judgement"`` → ``mark_pending_verdict`` (park) and surface the
  :class:`Escalation`. NEVER blocks: one parked cluster does not stop the loop
  from resubmitting another cluster — or another run.

Safety (mirrors auto-resume's idiom exactly):

* **Opt-in, default OFF** — a run whose record did not set
  ``auto_recover_on_failure`` computes its verdict but takes **no** side effect
  (no resubmit, no park). The verdict is still surfaced as data (#283: no
  agent-facing field bypasses a safety step).
* **Hard-capped** — ``auto_recover_count < max_auto_recovers`` is the backstop;
  a code verdict over the cap parks instead of resubmitting.

``preempted`` clusters are SKIPPED: they keep routing through the existing
``auto_resume_flow`` path (and the resolver's ``_DETERMINISTIC`` set
deliberately excludes ``preempted``), so this composite never double-handles
them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._wire.fixtures.escalation import CandidateAction, Escalation
from hpc_agent.ops.recover.failures_atom import fetch_failures as _fetch_failures
from hpc_agent.ops.recover.features_glue import (
    build_escalation_cluster,
    build_failure_features,
)
from hpc_agent.ops.recover.resolve import resolve as _resolve
from hpc_agent.ops.recover_flow import resubmit_flow as _resubmit_flow
from hpc_agent.state.journal import load_run, mark_pending_verdict, update_run_status
from hpc_agent.state.pack_declarations import resolve_failure_patterns

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from hpc_agent._wire.fixtures.escalation import EscalationCluster
    from hpc_agent._wire.fixtures.failure_features import FailureFeatures
    from hpc_agent.state.run_record import RunRecord

__all__ = [
    "ClusterOutcome",
    "ResolveAndRecoverOutcome",
    "maybe_resolve_and_recover",
]


@dataclass(frozen=True)
class ClusterOutcome:
    """The disposition of one failure cluster after resolve-and-recover.

    ``disposition`` is one of:

    * ``"resubmitted"`` — a ``decided_by="code"`` verdict auto-acted (a
      ``resubmit_flow`` fired with the refined overrides).
    * ``"held"`` — a ``decided_by="judgement"`` verdict (or a code verdict the
      cap/opt-out blocked from acting) was parked via ``mark_pending_verdict``.
    * ``"verdict_only"`` — opt-in OFF: the verdict was computed and surfaced but
      no side effect was taken (no resubmit, no park).
    * ``"skipped"`` — a ``preempted`` cluster left to the auto-resume path, or
      a cluster whose ``error_class`` the wire ``FailureFeatures`` model cannot
      validate (vocabulary gap — left for human triage rather than crashing
      the monitor tick; the reason names the contract test to extend).

    ``decided_by`` mirrors the resolver verdict (``"code"`` | ``"judgement"``)
    for clusters that were resolved; ``None`` for a skipped cluster.
    ``escalation`` carries the :class:`Escalation` block for a held /
    verdict-only judgement cluster so a caller can surface it verbatim.
    """

    fingerprint: str | None
    error_class: str | None
    task_ids: tuple[Any, ...]
    disposition: str
    decided_by: str | None = None
    reason: str = ""
    overrides: dict[str, Any] | None = None
    escalation: Escalation | None = None
    new_job_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ResolveAndRecoverOutcome:
    """Result of consulting (and possibly firing) the resolve-and-recover composite.

    Mirrors :class:`hpc_agent.ops.auto_resume_flow.AutoResumeOutcome`'s role: a
    structured, side-effect-free-to-read summary the monitor / campaign loop
    routes on. ``clusters`` lists every fetched cluster's disposition;
    ``resubmitted`` / ``held`` / ``skipped`` are convenience projections.
    ``auto_recover_count`` is the run's post-call counter.
    """

    run_id: str
    clusters: tuple[ClusterOutcome, ...] = ()
    reason: str = ""
    auto_recover_count: int = 0

    @property
    def resubmitted(self) -> tuple[ClusterOutcome, ...]:
        return tuple(c for c in self.clusters if c.disposition == "resubmitted")

    @property
    def held(self) -> tuple[ClusterOutcome, ...]:
        return tuple(c for c in self.clusters if c.disposition == "held")

    @property
    def skipped(self) -> tuple[ClusterOutcome, ...]:
        return tuple(c for c in self.clusters if c.disposition == "skipped")


def _fetch_clusters(
    experiment_dir: Path,
    run_id: str,
    failures_fetcher: Callable[..., dict[str, Any]],
) -> tuple[list[dict[str, Any]] | None, str]:
    """Return ``(clusters, reason)`` from the cluster failure report.

    ``(None, reason)`` when the report could not be fetched (SSH / cluster
    error) so the composite surfaces the reason rather than crashing the monitor
    loop — the same graceful-escalate posture as ``auto_resume_flow``.
    """
    try:
        report = failures_fetcher(experiment_dir=experiment_dir, run_id=run_id)
    except (errors.HpcError, OSError, TimeoutError) as exc:
        return None, f"could not fetch cluster failures for auto-recover: {exc}"
    clusters = report.get("clusters") if isinstance(report, dict) else None
    if not isinstance(clusters, list):
        return [], ""
    return clusters, ""


def _read_sidecar(experiment_dir: Path, run_id: str) -> dict[str, Any] | None:
    """Best-effort read of the run sidecar for ``resource_spec`` sourcing.

    A missing / unreadable sidecar is not fatal — the features glue simply
    yields an empty ``resource_spec`` (the resolver then falls back to its
    context-free catalog fix), so we return ``None`` rather than raising.
    """
    try:
        from hpc_agent.state.runs import read_run_sidecar
    except ImportError:  # pragma: no cover - import guard mirrors failures_atom
        return None
    try:
        return read_run_sidecar(experiment_dir, run_id)
    except (FileNotFoundError, OSError, ValueError, errors.HpcError):
        return None


def _coerce_task_ids(task_ids: list[Any]) -> list[int] | None:
    """Coerce cluster task ids to the ``list[int]`` ``resubmit_flow`` requires.

    Returns ``None`` if ANY id is not int-coercible, so the caller escalates the
    whole cluster rather than (a) crashing the unattended loop on a bad id or
    (b) silently resubmitting only the coercible subset. ``build_escalation_cluster``
    stringifies defensively for the escalation model, so the surfaced verdict
    still carries every task id even when the resubmit side cannot.
    """
    out: list[int] = []
    for t in task_ids:
        if isinstance(t, bool):  # bool is an int subclass — not a task id
            return None
        try:
            out.append(int(t))
        except (TypeError, ValueError):
            return None
    return out


def _concrete_overrides(
    action: dict[str, Any], resources: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Translate a resolver suggested-fix into concrete ``resubmit_flow`` overrides.

    ``resubmit_flow`` (via ``render_overrides_to_extra_flags``) applies only
    concrete scheduler knobs (``mem_mb`` / ``walltime_sec`` / ``gpus`` /
    ``cpus``), but the resolver emits a suggested-fix shape
    (``{action, factor, knob}``). This scales the relevant *current* resource —
    read from the run sidecar's ``resources`` block — by the fix's ``factor``:

    * ``increase-mem`` / ``increase-mem-per-gpu`` → ``mem_mb`` × factor
    * ``increase-walltime``                       → ``walltime_sec`` × factor
    * ``retry-on-different-node``                  → ``{}`` (a plain resubmit IS
      the fix — a fresh dispatch usually lands on a different node — so no
      override is needed)

    Returns ``None`` for a fix ``resubmit_flow`` cannot enact: the
    parallelism/width fixes (``increase-parallelism`` / ``reduce-width``) change
    a *task kwarg* (``tp_size`` / ``batch_size``), not a scheduler flag, and a
    factor fix with no current resource to scale. The caller surfaces those as a
    recommendation instead of resubmitting an unchanged config.
    """
    verb = action.get("action")
    if verb == "retry-on-different-node":
        return {}
    factor = action.get("factor")
    if not isinstance(factor, (int, float)) or isinstance(factor, bool) or factor <= 0:
        return None
    res = resources or {}
    if verb in ("increase-mem", "increase-mem-per-gpu"):
        cur = res.get("mem_mb")
        if isinstance(cur, int) and not isinstance(cur, bool) and cur > 0:
            return {"mem_mb": math.ceil(cur * factor)}
        return None
    if verb == "increase-walltime":
        cur = res.get("walltime_sec")
        if isinstance(cur, int) and not isinstance(cur, bool) and cur > 0:
            return {"walltime_sec": math.ceil(cur * factor)}
        return None
    # increase-parallelism / reduce-width change a task kwarg, not a scheduler
    # flag — resubmit_flow cannot enact them; the caller escalates.
    return None


def _code_escalation(
    action: dict[str, Any],
    features: FailureFeatures,
    esc_cluster: EscalationCluster,
    reason: str,
) -> Escalation:
    """Build a ``decided_by="code"`` escalation carrying a deterministic fix that
    ``resubmit_flow`` cannot auto-apply, surfaced as a single recommended
    :class:`CandidateAction` for manual action (the escalation model's documented
    ``decided_by="code"`` case)."""
    verb = str(action.get("action") or "")
    params = {k: v for k, v in action.items() if k != "action"}
    return Escalation(
        decided_by="code",
        reason=reason,
        failure_features=features,
        candidate_actions=[
            CandidateAction(
                action=verb,
                params=params,
                source="policy",
                rationale="deterministic fix; not auto-applicable via resubmit_flow",
            )
        ],
        cluster=esc_cluster,
    )


def _park_outcome(
    experiment_dir: Path,
    run_id: str,
    *,
    escalation: Escalation | None,
    reason: str,
    error_class: str | None,
    task_ids: tuple[Any, ...],
    fingerprint: str | None,
    opt_in: bool,
    decided_by: str,
) -> ClusterOutcome:
    """Park (opt-in ON) or just surface (opt-in OFF) an escalation.

    Shared by the ``judgement`` verdict branch and the ``code``-but-not-applicable
    branch. Opt-in ON → ``mark_pending_verdict`` (the run drops out of the
    campaign loop's not-done set, never blocking other work) + ``held``. Opt-in
    OFF → ``verdict_only`` (the decision-as-data is still computed; #283).
    """
    if not opt_in:
        return ClusterOutcome(
            fingerprint=fingerprint,
            error_class=error_class,
            task_ids=task_ids,
            disposition="verdict_only",
            decided_by=decided_by,
            reason=reason,
            escalation=escalation,
        )
    if escalation is not None:
        # The state layer is pure I/O — hand it the dumped dict, never the model.
        mark_pending_verdict(experiment_dir, run_id, escalation=escalation.model_dump())
    return ClusterOutcome(
        fingerprint=fingerprint,
        error_class=error_class,
        task_ids=task_ids,
        disposition="held",
        decided_by=decided_by,
        reason=reason,
        escalation=escalation,
    )


def maybe_resolve_and_recover(
    experiment_dir: Path,
    run_id: str,
    *,
    record: RunRecord | None = None,
    max_code_attempts: int = 1,
    resubmit: Callable[..., Any] = _resubmit_flow,
    failures_fetcher: Callable[..., dict[str, Any]] = _fetch_failures,
) -> ResolveAndRecoverOutcome:
    """Resolve every failure cluster for *run_id* and auto-act per the #240 policy.

    Fetches the cluster-authoritative failure report, then for each cluster
    (skipping ``preempted`` — the auto-resume path owns those) builds the #230
    evidence vector, calls :func:`resolve`, and routes the verdict:

    * ``code`` + opt-in ON + under cap → ``resubmit_flow`` with the refined
      overrides, bumping ``auto_recover_count``.
    * ``code`` + over cap → park (the cap is the backstop; we do not loop a fix).
    * ``judgement`` + opt-in ON → ``mark_pending_verdict`` (park) + surface the
      escalation. One parked cluster never blocks resubmit of another.
    * opt-in OFF → compute + surface the verdict, take no side effect (#283).

    *resubmit* and *failures_fetcher* are injection seams for tests, exactly as
    in :func:`hpc_agent.ops.auto_resume_flow.maybe_auto_resume`.
    """
    if record is None:
        record = load_run(experiment_dir, run_id)
    if record is None:
        return ResolveAndRecoverOutcome(run_id, reason=f"no journal record for {run_id!r}")

    clusters, fetch_reason = _fetch_clusters(experiment_dir, run_id, failures_fetcher)
    if clusters is None:
        return ResolveAndRecoverOutcome(
            run_id,
            reason=fetch_reason,
            auto_recover_count=int(record.auto_recover_count),
        )

    opt_in = bool(record.auto_recover_on_failure)
    sidecar = _read_sidecar(experiment_dir, run_id) if clusters else None
    resources = sidecar.get("resources") if isinstance(sidecar, dict) else None

    # S2 domain-pack seam: resolve every opted-in pack's failure_patterns ONCE (the
    # ops-root caller is the pack-declaration boundary; the features glue stays
    # pack-ignorant, receiving the typed opaque declarations). A repo that never
    # opted in returns [] with zero probes beyond interview.json (the D7 silence),
    # so an un-opted-in run's evidence vectors are byte-identical to the pre-packs
    # shape. The declarations ride into build_failure_features, which COUNTS hits
    # as evidence and NEVER maps a hit to a category/action/retry.
    #
    # T8 seam: the "pack" decision-journal scope kind + its records reader land
    # separately; until then resolve_failure_patterns reads the opt-in shape-only
    # (an opted-in repo with no current bind is loud by design, never a silent
    # pass), and this call gains records_reader=... when that scope kind exists.
    #
    # F25: pack-declaration staleness (SpecInvalid — a moved/edited manifest, a
    # superseded receipt with no current bind, a malformed entry) is loud by
    # design on the SUBMIT paths; the monitor's terminal-FAILED tick is the WRONG
    # place to enforce it. This resolution runs inside monitor_flow's unguarded
    # FAILED branch, so an unguarded raise here kills a multi-hour detached watch
    # BEFORE mark_terminal runs, stranding the run in-flight and re-crashing on
    # every re-watch. Degrade to no patterns (the features glue only COUNTS
    # pattern hits — losing them degrades evidence, not correctness) and surface
    # the reason, rather than letting unrelated pack state wedge failure
    # classification at the exact moment it is needed.
    failure_patterns: list[Any] = []
    if clusters:
        try:
            failure_patterns = resolve_failure_patterns(experiment_dir)
        except errors.HpcError as exc:
            import logging

            logging.getLogger(__name__).warning(
                "resolve-and-recover for run_id=%s: pack failure-pattern "
                "resolution failed (%s); degrading to NO failure patterns for this "
                "tick — the recovery verdict proceeds on the un-pack-augmented "
                "evidence vector. Fix the pack bind on a submit path, not here.",
                run_id,
                exc,
            )

    outcomes: list[ClusterOutcome] = []
    count = int(record.auto_recover_count)
    cap = int(record.max_auto_recovers)

    for cluster in clusters:
        error_class = cluster.get("error_class")
        task_ids = tuple(cluster.get("task_ids") or [])
        fingerprint = cluster.get("fingerprint")

        # Preempted clusters keep routing through the existing auto-resume path;
        # the resolver's _DETERMINISTIC set excludes ``preempted`` for the same
        # reason. Never double-handle.
        if error_class == "preempted":
            outcomes.append(
                ClusterOutcome(
                    fingerprint=fingerprint,
                    error_class=error_class,
                    task_ids=task_ids,
                    disposition="skipped",
                    reason="preempted: handled by the auto-resume path",
                )
            )
            continue

        # Defense-in-depth under the widened wire FailureCategory (bug-sweep #2):
        # a cluster whose error_class the wire model STILL cannot validate (a
        # future catalog row, a leaked sentinel) must degrade to a per-cluster
        # skipped outcome — never propagate and kill the whole monitor
        # terminal-FAILED tick, which fires exactly when the operator needs it.
        from pydantic import ValidationError

        try:
            features = build_failure_features(
                cluster, record=record, sidecar=sidecar, failure_patterns=failure_patterns
            )
        except ValidationError as exc:
            outcomes.append(
                ClusterOutcome(
                    fingerprint=fingerprint,
                    error_class=error_class,
                    task_ids=task_ids,
                    disposition="skipped",
                    reason=(
                        f"failure-features vocabulary gap: {exc.error_count()} validation "
                        f"error(s) building features for error_class={error_class!r} — "
                        "widen the wire FailureCategory (contract test: "
                        "test_failure_category_covers_classifier); cluster left for "
                        "human triage"
                    ),
                )
            )
            continue
        esc_cluster = build_escalation_cluster(cluster, run_id=run_id)
        resolution = _resolve(features, cluster=esc_cluster, max_code_attempts=max_code_attempts)

        if resolution.decided_by == "code":
            outcome, count = _act_on_code(
                experiment_dir,
                run_id,
                record=record,
                cluster=cluster,
                resolution_action=resolution.action or {},
                features=features,
                esc_cluster=esc_cluster,
                resources=resources,
                error_class=error_class,
                task_ids=task_ids,
                fingerprint=fingerprint,
                opt_in=opt_in,
                count=count,
                cap=cap,
                resubmit=resubmit,
            )
            outcomes.append(outcome)
            continue

        # judgement verdict — park (or, opt-out, surface only).
        outcomes.append(
            _park_outcome(
                experiment_dir,
                run_id,
                escalation=resolution.escalation,
                reason=resolution.reason,
                error_class=error_class,
                task_ids=task_ids,
                fingerprint=fingerprint,
                opt_in=opt_in,
                decided_by="judgement",
            )
        )

    return ResolveAndRecoverOutcome(
        run_id,
        clusters=tuple(outcomes),
        auto_recover_count=count,
    )


def _act_on_code(
    experiment_dir: Path,
    run_id: str,
    *,
    record: RunRecord,
    cluster: dict[str, Any],
    resolution_action: dict[str, Any],
    features: FailureFeatures,
    esc_cluster: EscalationCluster,
    resources: dict[str, Any] | None,
    error_class: str | None,
    task_ids: tuple[Any, ...],
    fingerprint: str | None,
    opt_in: bool,
    count: int,
    cap: int,
    resubmit: Callable[..., Any],
) -> tuple[ClusterOutcome, int]:
    """Route a ``decided_by="code"`` verdict. Returns ``(outcome, new_count)``.

    The resolver's suggested-fix is translated to concrete resubmit overrides
    (:func:`_concrete_overrides`). A fix ``resubmit_flow`` cannot enact — a
    parallelism/width fix (a *task kwarg*, not a scheduler flag), a ``factor``
    with no current resource to scale, or a non-integer task id — is **surfaced
    as a ``decided_by="code"`` escalation** rather than resubmitted-identical
    (which would burn the cap re-running the failing config) or crashed on.
    Otherwise: opt-in OFF → ``verdict_only``; over cap → ``held``; else
    ``resubmit_flow`` with the concrete overrides, bumping the counter on a real
    (non-deduped) submit.
    """
    verb = str(resolution_action.get("action") or "")
    failed_task_ids = _coerce_task_ids(list(cluster.get("task_ids") or []))
    overrides = (
        _concrete_overrides(resolution_action, resources) if failed_task_ids is not None else None
    )

    if failed_task_ids is None or overrides is None:
        # A deterministic fix we cannot enact via resubmit_flow — surface it
        # (decided_by="code") instead of resubmitting an unchanged config or
        # crashing the unattended loop on a bad task id.
        why = (
            "non-integer task id"
            if failed_task_ids is None
            else "changes a task kwarg or has no scalable resource"
        )
        reason = (
            f"{error_class}: deterministic fix {verb!r} not auto-applicable "
            f"({why}); surfaced for manual action"
        )
        escalation = _code_escalation(resolution_action, features, esc_cluster, reason)
        return (
            _park_outcome(
                experiment_dir,
                run_id,
                escalation=escalation,
                reason=reason,
                error_class=error_class,
                task_ids=task_ids,
                fingerprint=fingerprint,
                opt_in=opt_in,
                decided_by="code",
            ),
            count,
        )

    if not opt_in:
        # #283: opt-out still surfaces the verdict-as-data; no resubmit.
        return (
            ClusterOutcome(
                fingerprint=fingerprint,
                error_class=error_class,
                task_ids=task_ids,
                disposition="verdict_only",
                decided_by="code",
                reason="auto_recover_on_failure not enabled",
                overrides=overrides,
            ),
            count,
        )

    if count >= cap:
        # Cap is the backstop — park rather than loop a deterministic fix that
        # the cap says has run its budget.
        return (
            ClusterOutcome(
                fingerprint=fingerprint,
                error_class=error_class,
                task_ids=task_ids,
                disposition="held",
                decided_by="code",
                reason=f"auto-recover cap reached ({count}/{cap})",
                overrides=overrides,
            ),
            count,
        )

    # request_id folds in the current count so each cap-loop attempt is a
    # distinct request (mirrors auto_resume_flow): two genuine recoveries of the
    # same set must not dedup against each other.
    request_id = f"auto_recover_{run_id}_{count}"
    try:
        result = resubmit(
            experiment_dir,
            run_id,
            failed_task_ids=failed_task_ids,
            category=error_class,
            overrides=overrides,
            from_checkpoint=True,
            submit_to_cluster=True,
            script=record.script,
            backend=record.backend,
            job_name=record.job_name,
            job_env=dict(record.job_env),
            request_id=request_id,
        )
    except (errors.HpcError, OSError) as exc:
        # F26: the resubmit leg is a qsub-over-ssh — a transient SshUnreachable /
        # RemoteCommandFailed / OSError at exactly the moment the monitor already
        # survived a dozen tolerated poll blips must NOT propagate through
        # monitor_flow's terminal-FAILED branch and kill the detached watch after
        # nothing was even put on the cluster. Park the cluster as HELD with the
        # reason (never bumping the cap — nothing landed) so the monitor falls
        # through to the normal FAILED surface instead of dying; the module's
        # never-blocks policy applies to the resubmit call itself, not only the
        # status polls in the same loop.
        import logging

        logging.getLogger(__name__).error(
            "auto-recover for run_id=%s: resubmit of %s failed (%s); parking the "
            "cluster as held and surfacing the reason rather than crashing the "
            "monitor's terminal-FAILED tick.",
            run_id,
            error_class,
            exc,
        )
        return (
            ClusterOutcome(
                fingerprint=fingerprint,
                error_class=error_class,
                task_ids=task_ids,
                disposition="held",
                decided_by="code",
                reason=f"{error_class}: auto-recover resubmit failed: {exc}",
                overrides=overrides,
            ),
            count,
        )

    deduped = bool(getattr(result, "deduped", False))
    new_count = count
    if not deduped:
        # A real resubmit fired — bump the cap counter. Fail CLOSED on a journal-
        # write failure (mirrors the auto_resume_flow twin): the work is ALREADY
        # on the cluster, so this attempt MUST count against the cap even when we
        # cannot persist the bump. An uncaught write failure here would (a) crash
        # the monitor's terminal-FAILED tick outright and (b) leave the counter
        # un-bumped — which LOOSENS the cap (the next watch reads the stale count
        # and fires an extra resubmit PAST the ceiling). Keep the in-memory bump
        # and log loudly rather than under-count or propagate.
        new_count += 1
        try:
            updated = update_run_status(experiment_dir, run_id, auto_recover_count=new_count)
            new_count = int(updated.auto_recover_count)
        except (errors.HpcError, OSError) as exc:
            import logging

            logging.getLogger(__name__).error(
                "auto-recover for run_id=%s fired a resubmit but FAILED to persist "
                "the cap-counter bump (%s); counting the attempt in-memory to fail "
                "CLOSED so the recover cap cannot loosen. The journal "
                "auto_recover_count may be stale-by-one until the next successful "
                "status write.",
                run_id,
                exc,
            )

    return (
        ClusterOutcome(
            fingerprint=fingerprint,
            error_class=error_class,
            task_ids=task_ids,
            disposition="resubmitted",
            decided_by="code",
            reason=f"{error_class}: auto-recovered ({verb})",
            overrides=overrides,
            new_job_ids=list(getattr(result, "new_job_ids", []) or []),
        ),
        new_count,
    )
