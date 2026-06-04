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

from hpc_agent._kernel.decision import Decision, decide, tally
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


# The decision point id this resolver decides, on the wire seam shared with
# DECISION_POINTS / WorkerDecision / the journal's verdict_history.
_POINT = "recover"


@dataclass(frozen=True)
class Resolution:
    """The resolver's verdict for one failure cluster.

    ``decided_by="code"`` carries the deterministic ``action`` (a
    ``suggested_fix``-shaped dict ready for ``resubmit_flow`` overrides) and
    no escalation. ``decided_by="judgement"`` carries an :class:`Escalation`
    block to hand off to the agentic layer (and no ``action`` — the verdict
    picks one of the candidates). The two are mutually exclusive.

    This is the domain view over the kernel's :class:`Decision`: the recovery
    layer wants ``action`` as a flat ``suggested_fix`` dict for resubmit
    overrides, so :meth:`from_decision` flattens the chosen
    :class:`CandidateAction` (``{action, **params}``). Everything else —
    the deterministic-vs-escalate control flow, the ``decided_by`` split,
    the tally — is the kernel's.
    """

    decided_by: Literal["code", "judgement"]
    action: dict[str, Any] | None = None
    escalation: Escalation | None = None
    reason: str = ""

    @classmethod
    def from_decision(cls, decision: Decision[Any]) -> Resolution:
        """Adapt a kernel :class:`Decision` into the recovery-domain view."""
        action: dict[str, Any] | None = None
        if decision.chosen is not None:
            action = {"action": decision.chosen.action, **decision.chosen.params}
        return cls(
            decided_by=decision.decided_by,
            action=action,
            escalation=decision.escalation,
            reason=decision.reason,
        )


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


def _error_class(features: FailureFeatures) -> str:
    return str(features.error_class) if features.error_class is not None else "unknown"


def _deterministic_fix(
    error_class: str, resource_spec: dict[str, Any] | None, *, first_attempt: bool
) -> tuple[dict[str, Any], str]:
    """The context-refined ``(suggested_fix, strategy)`` for a deterministic class."""
    if error_class == "gpu_oom":
        action = _gpu_oom_action(resource_spec, first_attempt=first_attempt)
        return action, str(action["action"])
    if error_class == "system_oom":
        return {"action": "increase-mem", "factor": 1.5}, "increase-mem"
    if error_class == "walltime":
        # first_attempt = structural underestimate (double it); after progress
        # = a long run that nearly finished (a smaller bump usually suffices).
        factor = 2.0 if first_attempt else 1.5
        return {"action": "increase-walltime", "factor": factor}, "increase-walltime"
    # node_failure
    return {"action": "retry-on-different-node"}, "retry-on-different-node"


def _code_rule(
    features: FailureFeatures, *, first_attempt: bool, max_code_attempts: int
) -> CandidateAction | None:
    """The deterministic rule: the context-refined fix, or ``None`` to abstain.

    Abstains (→ escalation) for inherently-ambiguous classes, classes with no
    rule, and any deterministic fix already exhausted this episode — the
    #234 fall-through that stops looping a fix that isn't working.
    """
    error_class = _error_class(features)
    if error_class in _ALWAYS_JUDGEMENT or error_class not in _DETERMINISTIC:
        return None
    action, strategy = _deterministic_fix(
        error_class, features.resource_spec, first_attempt=first_attempt
    )
    if _strategy_exhausted(features, strategy, cap=max_code_attempts):
        return None
    params = {k: v for k, v in action.items() if k != "action"}
    return CandidateAction(
        action=strategy, params=params, source="policy", rationale=f"{error_class}: {strategy}"
    )


def _abstain(
    features: FailureFeatures,
    *,
    cluster: EscalationCluster | None,
    first_attempt: bool,
) -> Escalation:
    """Build the escalation for a cluster no deterministic rule resolved.

    Three shapes, keyed on *why* the rules abstained: an inherently-ambiguous
    class (the named #234 Tier-2 cases), a class with no rule (the unknown
    escape hatch), or a deterministic fix already exhausted this episode.
    """
    error_class = _error_class(features)
    if error_class in _ALWAYS_JUDGEMENT:
        reason = f"{error_class}: ambiguous, needs a decision"
        candidates = _judgement_candidates(error_class)
    elif error_class not in _DETERMINISTIC:
        reason = f"{error_class}: no deterministic rule"
        candidates = [CandidateAction(action="user-debug", source="catalog")]
    else:
        # A deterministic class that abstained → its fix was exhausted.
        _, strategy = _deterministic_fix(
            error_class, features.resource_spec, first_attempt=first_attempt
        )
        reason = f"{error_class}: deterministic fix '{strategy}' exhausted this episode"
        candidates = [CandidateAction(action=strategy, source="policy", rationale="already tried")]
    return Escalation(
        decided_by="judgement",
        reason=reason,
        failure_features=features,
        candidate_actions=candidates,
        cluster=cluster,
    )


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

    The recovery rules + the abstain candidates are this module's domain; the
    try-rules-then-escalate control flow is the shared
    :func:`hpc_agent._kernel.decision.decide` kernel.
    """
    first_attempt = bool(
        features.temporal_context is not None and features.temporal_context.phase == "first_attempt"
    )
    decision = decide(
        _POINT,
        features,
        rules=[
            lambda f: _code_rule(
                f, first_attempt=first_attempt, max_code_attempts=max_code_attempts
            )
        ],
        on_abstain=lambda f: _abstain(f, cluster=cluster, first_attempt=first_attempt),
    )
    return Resolution.from_decision(decision)


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
    Returns ``{"code": n, "judgement": m}``. Delegates to the kernel
    :func:`hpc_agent._kernel.decision.tally` — a :class:`Resolution` exposes
    the same ``decided_by`` the kernel counts.
    """
    return tally(resolutions)
