"""Context-keyed failure resolver — the widened deterministic key (#234).

The existing deterministic layer keys a fix on ``error_class`` *alone*
(``failure_signatures.classify`` → ``DEFAULT_AUTO_RETRY_POLICY[category]``),
so it emits one static fix per signature. This module widens that key to
``(error_class, temporal_context, resource_spec)`` — the discriminating
fields of the #230 ``failure_features`` evidence vector — so the *same*
signature resolves to the *right* fix: an OOM at ``tp_size=2`` (model
already sharded across GPUs → bumping per-GPU memory won't help → reshard)
is a different fix from an OOM at a large batch width (shrink the width),
even though both classify as ``gpu_oom``.

**This is one resolver with a wider key, not a second tier.** Adding
context *migrates cases out of the LLM's domain*: a signature the flat
classifier had to escalate becomes a deterministic rule here. Only the
genuinely-ambiguous residue — ``unknown``, ``code_bug`` (real vs.
transient), ``segv``, an exhausted retry strategy — escalates to the
agentic layer, tagged ``decided_by="judgement"`` through the unified
escalation block (#231).

Grow this empirically (the #234 caution): a rule is justified only when
the same context repeatedly resolves the same way. The handful here are
the ones the failure_features fields make unambiguous today; the
``decided_by`` tally is the signal for promoting the next one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from hpc_agent._wire.fixtures.escalation import (
    CandidateAction,
    Escalation,
    EscalationCluster,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from hpc_agent._wire.fixtures.failure_features import FailureFeatures

__all__ = ["Resolution", "resolve", "tally_decisions"]

# error_class values the deterministic layer can resolve on its own (given
# enough context). Everything else — and any of these once the context is
# missing or the retry budget is spent — escalates to judgement.
_DETERMINISTIC: frozenset[str] = frozenset({"gpu_oom", "system_oom", "walltime", "node_failure"})

# error_class values that are *inherently* a judgement call — the classifier
# matched, but the right action is genuinely ambiguous and context cannot
# disambiguate it. These are #234's named Tier-2 cases.
_ALWAYS_JUDGEMENT: frozenset[str] = frozenset({"segv", "queue_stall", "code_bug", "unknown"})

# resource_spec keys that signal model/tensor/pipeline parallelism — when the
# degree is >1 the model is already sharded across GPUs, so a gpu_oom is a
# per-GPU-capacity problem that more memory per GPU won't fix; reshard instead.
_PARALLELISM_KEYS: tuple[str, ...] = (
    "tp_size",
    "tensor_parallel",
    "tensor_parallel_size",
    "pp_size",
    "pipeline_parallel",
    "model_parallel",
    "world_size",
)

# resource_spec keys that are a width/batch knob — a gpu_oom on a large value
# here (especially on the first attempt) points at an over-wide config that
# should be shrunk rather than fed more memory.
_WIDTH_KEYS: tuple[str, ...] = (
    "batch_size",
    "micro_batch",
    "batch",
    "n",
    "num_samples",
    "width",
)


@dataclass(frozen=True)
class Resolution:
    """The resolver's verdict for one failure cluster.

    ``decided_by="code"`` carries the deterministic ``action`` (a
    ``suggested_fix``-shaped dict ready for ``resubmit_flow`` overrides) and
    no escalation. ``decided_by="judgement"`` carries an :class:`Escalation`
    block to hand off to the agentic layer (and no ``action`` — the verdict
    picks one of the candidates). The two are mutually exclusive.
    """

    decided_by: Literal["code", "judgement"]
    action: dict[str, Any] | None = None
    escalation: Escalation | None = None
    reason: str = ""


def _degree(resource_spec: dict[str, Any] | None, keys: Iterable[str]) -> int | None:
    """Largest integer value among *keys* present in *resource_spec*, or None."""
    if not resource_spec:
        return None
    best: int | None = None
    for k in keys:
        v = resource_spec.get(k)
        if isinstance(v, bool):  # bool is an int subclass — exclude it
            continue
        if isinstance(v, int):
            best = v if best is None else max(best, v)
    return best


def _strategy_exhausted(features: FailureFeatures, strategy: str, *, cap: int) -> bool:
    """True when the deterministic *strategy* has already been tried (or the
    per-episode attempt cap is spent) — the signal to stop looping a fix that
    isn't working and escalate instead."""
    ate = features.attempts_this_episode
    if ate is None:
        return False
    if ate.count >= cap:
        return True
    return bool(ate.strategies and strategy in ate.strategies)


def _gpu_oom_action(resource_spec: dict[str, Any] | None, *, first_attempt: bool) -> dict[str, Any]:
    """Context-discriminated gpu_oom fix — the canonical OOM@tp_size vs
    OOM@width split."""
    parallel = _degree(resource_spec, _PARALLELISM_KEYS)
    if parallel is not None and parallel > 1:
        # Already sharded across GPUs — more memory per GPU won't help; widen
        # the shard instead.
        return {"action": "increase-parallelism", "knob": "tp_size", "factor": 2}
    width = _degree(resource_spec, _WIDTH_KEYS)
    if width is not None and width > 1 and first_attempt:
        # Structurally over-wide on the first attempt — shrink rather than
        # throw memory at it.
        return {"action": "reduce-width", "factor": 0.5}
    # No discriminating context → fall back to the catalog's flat fix.
    return {"action": "increase-mem-per-gpu", "factor": 1.5}


def _escalate(
    features: FailureFeatures,
    *,
    reason: str,
    candidates: list[CandidateAction],
    cluster: EscalationCluster | None,
) -> Resolution:
    block = Escalation(
        decided_by="judgement",
        reason=reason,
        failure_features=features,
        candidate_actions=candidates,
        cluster=cluster,
    )
    return Resolution(decided_by="judgement", escalation=block, reason=reason)


def resolve(
    features: FailureFeatures,
    *,
    cluster: EscalationCluster | None = None,
    max_code_attempts: int = 1,
) -> Resolution:
    """Resolve one failure cluster from its evidence vector (#234).

    Returns a deterministic :class:`Resolution` (``decided_by="code"``) when
    the widened ``(error_class, temporal_context, resource_spec)`` key yields
    an unambiguous fix; otherwise escalates (``decided_by="judgement"``) with
    an :class:`Escalation` block carrying the evidence, the candidate actions,
    and the affected-task *cluster* so a verdict fans back out per-task.

    *max_code_attempts* caps deterministic retries per episode — once spent,
    the cluster escalates rather than looping the same fix.
    """
    error_class = str(features.error_class) if features.error_class is not None else "unknown"
    resource_spec = features.resource_spec
    first_attempt = bool(
        features.temporal_context is not None and features.temporal_context.phase == "first_attempt"
    )

    # Inherently-ambiguous classes go straight to judgement.
    if error_class in _ALWAYS_JUDGEMENT:
        return _escalate(
            features,
            reason=f"{error_class}: ambiguous, needs a decision",
            candidates=_judgement_candidates(error_class),
            cluster=cluster,
        )

    if error_class not in _DETERMINISTIC:
        # Anything the resolver has no rule for (a class outside both sets) is
        # treated as the unknown escape hatch — escalate.
        return _escalate(
            features,
            reason=f"{error_class}: no deterministic rule",
            candidates=[CandidateAction(action="user-debug", source="catalog")],
            cluster=cluster,
        )

    # Deterministic classes — compute the context-refined action.
    if error_class == "gpu_oom":
        action = _gpu_oom_action(resource_spec, first_attempt=first_attempt)
        strategy = str(action["action"])
    elif error_class == "system_oom":
        action = {"action": "increase-mem", "factor": 1.5}
        strategy = "increase-mem"
    elif error_class == "walltime":
        # first_attempt = structural underestimate (double it); after progress
        # = a long run that nearly finished (a smaller bump usually suffices).
        factor = 2.0 if first_attempt else 1.5
        action = {"action": "increase-walltime", "factor": factor}
        strategy = "increase-walltime"
    else:  # node_failure
        action = {"action": "retry-on-different-node"}
        strategy = "retry-on-different-node"

    # If this exact deterministic strategy was already tried this episode (or
    # the attempt budget is spent), the fix isn't working — escalate instead
    # of looping it. This fall-through to judgement is the #234 health signal.
    if _strategy_exhausted(features, strategy, cap=max_code_attempts):
        return _escalate(
            features,
            reason=f"{error_class}: deterministic fix '{strategy}' exhausted this episode",
            candidates=[
                CandidateAction(action=strategy, source="policy", rationale="already tried")
            ],
            cluster=cluster,
        )

    return Resolution(decided_by="code", action=action, reason=f"{error_class}: {strategy}")


def _judgement_candidates(error_class: str) -> list[CandidateAction]:
    """The starting options offered to the agentic layer for an ambiguous class."""
    if error_class == "code_bug":
        return [
            CandidateAction(action="retry", source="policy", rationale="if transient"),
            CandidateAction(action="user-debug", source="catalog", rationale="if a real bug"),
        ]
    if error_class == "segv":
        return [
            CandidateAction(
                action="retry-on-different-node", source="policy", rationale="if node-degraded"
            ),
            CandidateAction(action="user-debug", source="catalog", rationale="if a real bug"),
        ]
    if error_class == "queue_stall":
        return [CandidateAction(action="wait", source="policy", rationale="scheduler backlog")]
    return [CandidateAction(action="user-debug", source="catalog")]


def tally_decisions(resolutions: Iterable[Resolution]) -> dict[str, int]:
    """Count code vs. judgement verdicts — the #234 promotion health signal.

    A signature that repeatedly lands in ``judgement`` is the trigger to
    promote a context-keyed rule for it (manually — no automatic codegen).
    Returns ``{"code": n, "judgement": m}``.
    """
    counts = {"code": 0, "judgement": 0}
    for r in resolutions:
        counts[r.decided_by] += 1
    return counts
