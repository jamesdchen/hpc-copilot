"""Per-cluster ``failure_features`` glue — one ``fetch_failures`` cluster → a
:class:`FailureFeatures` evidence vector the resolver pattern-matches on (#240).

This is the PURE adapter the #240 flow-wiring needs: it bridges the two shapes
the resolver sits between — the cluster-authoritative failure report
(``ops.recover.runner_failures.cluster_failures_by_fingerprint`` entries, each
``{error_class/category, fingerprint, task_ids, …}``) on one side, and the
#230 ``failure_features`` model on the other. ``resolve()`` keys on the widened
``(error_class, temporal_context, resource_spec)`` tuple, so this module's whole
job is to populate those three fields (plus ``attempts_this_episode`` so the
``_strategy_exhausted`` fall-through can fire) from the run's on-disk state.

It also assembles the :class:`EscalationCluster` provenance block so a
``decided_by="judgement"`` verdict fans back out to the affected tasks.

Shape decisions (#240's "several reasonable shapes" note — documented so the
choice is auditable):

* **resource_spec** is sourced from the run sidecar via
  :func:`hpc_agent.state.runs.read_run_sidecar`. The reliably-present source is
  the sidecar's ``resources`` dict (cpus / mem / gpus / gpu_type / walltime —
  the scheduler-level knobs ``write_run_sidecar`` always records). Task-level
  sweep kwargs (``tp_size`` / ``batch_size`` / ``n`` — the dispatcher's
  ``HPC_KW_*`` namespace) are NOT stored verbatim on the sidecar today, so a
  producer that wants the OOM@tp_size-vs-width discriminator to fire records
  them in the sidecar's free-form ``extra.spec_kwargs`` pocket; we merge that
  pocket over ``resources`` when present. Values are passed through
  **as-written** (no int normalization): ``resolve._degree`` already coerces
  int-like strings, so passthrough is the safe, lossless choice and avoids this
  glue silently rewriting an audit value.
* **temporal_context.phase** is ``"unknown"``. The recover seam has no
  *progress* signal, and ``phase`` is defined as one (``first_attempt`` =
  failed before any unit of work succeeded; ``after_progress`` = some work
  succeeded first) — which a retry count is NOT (a first-attempt task can OOM
  after processing most of its data; a structural failure can have been retried
  already). ``resolve()`` branches the reshard/walltime call on ``first_attempt``,
  so deriving the phase from ``retries`` would mis-key the exact discrimination
  #234 exists for; ``"unknown"`` is ``resolve()``'s conservative
  (not-``first_attempt``) read. The retry count is carried in
  ``attempts_this_episode`` instead. Deriving a real progress signal from the
  #294 checkpoint / partial-output machinery is a follow-up.
* **attempts_this_episode.count** is the max prior ``attempts`` among the
  cluster's tasks; **.strategies** is the de-duplicated, order-preserving list
  of every prior ``category`` and override-derived action recorded in
  ``record.retries`` for those tasks — so ``resolve._strategy_exhausted`` can
  see that a deterministic fix was already tried and stop looping it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent._wire.fixtures.escalation import EscalationCluster
from hpc_agent._wire.fixtures.failure_features import FailureFeatures

if TYPE_CHECKING:
    from hpc_agent.state.run_record import RunRecord

__all__ = ["build_escalation_cluster", "build_failure_features"]


def _resource_spec_from_sidecar(sidecar: dict[str, Any] | None) -> dict[str, Any]:
    """Assemble the resolver's ``resource_spec`` from a run sidecar dict.

    Merges the scheduler-level ``resources`` block (cpus / mem / gpus /
    gpu_type / walltime) with an optional ``extra.spec_kwargs`` pocket holding
    the task-level sweep kwargs (``tp_size`` / ``batch_size`` / ``n``) — the
    pocket wins on a key collision, since a producer that records it is being
    explicit about the sweep degree. Values pass through as-written; the
    resolver's ``_degree`` coerces int-like strings.
    """
    spec: dict[str, Any] = {}
    if not sidecar:
        return spec
    resources = sidecar.get("resources")
    if isinstance(resources, dict):
        spec.update(resources)
    extra = sidecar.get("extra")
    if isinstance(extra, dict):
        kwargs = extra.get("spec_kwargs")
        if isinstance(kwargs, dict):
            spec.update(kwargs)
    return spec


def _retry_facts(record: RunRecord, task_ids: list[Any]) -> tuple[int, list[str]]:
    """Return ``(max_attempts, strategies)`` across *task_ids* from the journal.

    ``max_attempts`` is the largest prior ``attempts`` counter among the
    cluster's tasks (0 when none was ever retried). ``strategies`` is the
    de-duplicated, order-preserving union of every recorded ``category`` and
    override-derived action for those tasks — the exact strings
    ``resolve._strategy_exhausted`` checks against the deterministic fix name.
    """
    retries = record.retries or {}
    max_attempts = 0
    strategies: list[str] = []
    seen: set[str] = set()

    def _add(strategy: Any) -> None:
        if isinstance(strategy, str) and strategy and strategy not in seen:
            seen.add(strategy)
            strategies.append(strategy)

    for tid in task_ids:
        prior = retries.get(str(tid))
        if not isinstance(prior, dict):
            continue
        max_attempts = max(max_attempts, int(prior.get("attempts", 0) or 0))
        _add(prior.get("category"))
        overrides = prior.get("overrides")
        if isinstance(overrides, dict):
            # The override carries the action that was applied (the same
            # ``suggested_fix``-shaped dict the resolver emits as
            # ``Resolution.action``); record its verb so a re-resolve sees the
            # strategy was already tried.
            _add(overrides.get("action"))
    return max_attempts, strategies


def build_escalation_cluster(cluster: dict[str, Any], *, run_id: str) -> EscalationCluster:
    """Build the :class:`EscalationCluster` provenance for one failure cluster.

    Carries the ``fingerprint`` (the cluster key) and the per-task refs (as
    strings — the model's ``task_ids`` is ``list[str]``) so a
    ``decided_by="judgement"`` verdict fans back out to each affected task.
    """
    task_ids = [str(t) for t in (cluster.get("task_ids") or [])]
    return EscalationCluster(
        fingerprint=cluster.get("fingerprint"),
        run_id=run_id,
        task_ids=task_ids,
    )


def build_failure_features(
    cluster: dict[str, Any],
    *,
    record: RunRecord,
    sidecar: dict[str, Any] | None,
) -> FailureFeatures:
    """Map one ``fetch_failures`` cluster + the run state → a :class:`FailureFeatures`.

    PURE: takes the cluster dict, the run's :class:`RunRecord`, and its
    already-read sidecar dict (or ``None`` when unavailable), and returns the
    evidence vector ``resolve()`` keys on. See the module docstring for the
    shape rationale.

    * ``error_class`` — the cluster's ``error_class`` (the canonical
      ``FailureCategory`` ``cluster_failures_by_fingerprint`` derived from the
      one classifier).
    * ``resource_spec`` — :func:`_resource_spec_from_sidecar`.
    * ``temporal_context.phase`` — ``"unknown"`` (no progress signal at this
      seam; a retry count is not progress — see the module docstring).
    * ``attempts_this_episode`` — the max prior attempt count + the prior
      strategies, so the exhaustion fall-through can fire.
    """
    task_ids = list(cluster.get("task_ids") or [])
    resource_spec = _resource_spec_from_sidecar(sidecar)
    max_attempts, strategies = _retry_facts(record, task_ids)

    # phase="unknown": a retry count is not a progress signal (see the module
    # docstring) and resolve() reads "unknown" as not-first_attempt. The
    # retry count lands in attempts_this_episode below, not here.
    #
    # model_validate (not the FailureFeatures(...) ctor) so the nested dicts are
    # coerced into the typed _TemporalContext / _AttemptsThisEpisode submodels —
    # the same construction style as ops.recover.service.service_failure_features.
    # Bound to a typed local (not returned inline) so warn_return_any does not
    # trip on model_validate's Any return.
    features: FailureFeatures = FailureFeatures.model_validate(
        {
            "error_class": cluster.get("error_class"),
            "resource_spec": resource_spec or None,
            "temporal_context": {"phase": "unknown"},
            "attempts_this_episode": {
                "count": max_attempts,
                "strategies": strategies or None,
            },
        }
    )
    return features
