"""Service-dependency Tier 1: address passthrough + escalation contract (#231).

A sweep can depend on a companion service (a compile server, an inference
endpoint, …) the framework does NOT stand up — the cluster, an external
supervisor, or a user sbatch sidecar owns the process. We own the contract
for *consuming* it:

* **passthrough** — :func:`inject_service_env` threads an
  externally-provisioned service address into each task's environment the
  same way the dispatcher threads its ``HPC_KW_*`` kwargs. This rides on the
  env-injection mechanism that already exists (``dispatch.py`` builds the
  per-task env from ``os.environ`` + ``HPC_KW_*``); we just add a namespaced
  ``service_env`` source.
* **escalation** — :func:`service_failure_escalation` turns a service
  liveness/correctness probe into a ``failure_features``-carrying
  :class:`Escalation` (the #231 decision-as-data block), so a service
  failure routes through the decision path instead of surfacing as an opaque
  task failure. The non-obvious case it exists for is *silent rot*:
  liveness=pass, correctness=fail — "up but not ready" — which a port ping
  would miss. A health check must exercise the real path; that is
  irreducibly the caller's to define.

No lifecycle management here (Tier 2: ``up_command``/``health_check``/
``teardown`` — deferred until demonstrated demand). We do not provision,
we consume.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hpc_agent._wire.fixtures.escalation import Escalation, EscalationCluster
    from hpc_agent._wire.fixtures.failure_features import FailureFeatures

__all__ = [
    "SERVICE_ENV_NAMESPACE",
    "inject_service_env",
    "service_failure_features",
    "service_failure_escalation",
]

# Prefix every injected service var gets, so an address can never silently
# clobber an executor's $HOME/$PATH (the bare-uppercase footgun the HPC_KW_*
# namespacing already guards against on the kwargs side).
SERVICE_ENV_NAMESPACE = "HPC_SERVICE_"


def inject_service_env(env: dict[str, str], service_env: dict[str, Any] | None) -> dict[str, str]:
    """Thread an externally-provisioned service address into a task env.

    Each ``service_env`` entry ships as ``HPC_SERVICE_<KEY>`` (namespaced,
    collision-free), mirroring the dispatcher's ``HPC_KW_*`` contract.
    Returns *env* (mutated in place) for the caller's convenience. A
    ``None``/empty *service_env* is a clean no-op — sweeps without a service
    dependency are unaffected.
    """
    if not service_env:
        return env
    for key, value in service_env.items():
        env[f"{SERVICE_ENV_NAMESPACE}{key.upper()}"] = str(value)
    return env


def service_failure_features(
    *,
    liveness: str,
    correctness: str,
    detail: str | None = None,
    error_class_raw: str | None = None,
) -> FailureFeatures:
    """Build the ``failure_features`` evidence for a service failure.

    Separates the liveness signal (does it respond?) from the correctness
    signal (does a real request return a valid result?). The error_class is
    left at the ``unknown`` escape hatch unless the caller supplies a raw
    signature — a service failure is, by default, a decision for the agentic
    layer, not a known deterministic fix.
    """
    from hpc_agent._wire.fixtures.failure_features import FailureFeatures

    return FailureFeatures.model_validate(
        {
            "error_class": "unknown",
            "error_class_raw": error_class_raw,
            "liveness_vs_correctness": {
                "liveness": liveness,
                "correctness": correctness,
                "detail": detail,
            },
        }
    )


def service_failure_escalation(
    *,
    liveness: str,
    correctness: str,
    detail: str | None = None,
    error_class_raw: str | None = None,
    cluster: EscalationCluster | None = None,
) -> Escalation:
    """Route a service failure through the unified escalation block (#231).

    Produces a ``decided_by="judgement"`` :class:`Escalation` carrying the
    liveness/correctness evidence and a starting candidate set. The candidates
    differ by signal:

    * liveness=fail → the service is *down*; restarting it is the first move.
    * liveness=pass, correctness=fail → *silent rot* ("up but not ready"); a
      restart may or may not help, so it is offered alongside escalating to a
      human — this is the case a port ping misses and the reason the contract
      separates the two signals.
    """
    from hpc_agent._wire.fixtures.escalation import CandidateAction, Escalation

    features = service_failure_features(
        liveness=liveness,
        correctness=correctness,
        detail=detail,
        error_class_raw=error_class_raw,
    )
    if liveness == "fail":
        reason = "service down (liveness=fail)"
        candidates = [
            CandidateAction(action="restart-service", source="policy", rationale="liveness failed")
        ]
    elif correctness == "fail":
        reason = "service up but not ready (silent rot: liveness=pass, correctness=fail)"
        candidates = [
            CandidateAction(
                action="restart-service", source="policy", rationale="may clear bad state"
            ),
            CandidateAction(
                action="user-debug", source="catalog", rationale="real-path request failed"
            ),
        ]
    else:
        reason = "service degraded"
        candidates = [CandidateAction(action="user-debug", source="catalog")]

    return Escalation(
        decided_by="judgement",
        reason=reason,
        failure_features=features,
        candidate_actions=candidates,
        cluster=cluster,
    )
