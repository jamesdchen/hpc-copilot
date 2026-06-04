"""The primordial decision object — one evaluator every router calls.

Across the codebase, every *"which branch do we take"* point has the
same skeleton: run an ordered set of deterministic rules over an
evidence vector; the first rule that fires *resolves* the decision
(``decided_by="code"``, carrying the chosen action); if every rule
abstains, the point genuinely needs a decision the deterministic layer
cannot make, so it *escalates* (``decided_by="judgement"``, carrying an
:class:`Escalation` with the candidate actions a human/LLM reasons over).

``ops.recover.resolve`` is the fully-evolved instance of this skeleton
(failure recovery, with the context-keyed gpu_oom split and the
exhaustion fall-through); the four workflow routers
(``suggest-setup-action``, ``decide-monitor-arm``, ``campaign-advance``,
``resolve``) each re-implemented a fragment of it. This module is the
single object they call instead: the control flow + the
:class:`Decision` result type + the :func:`tally` promotion signal live
here once. The domain knowledge — *which* rules, *what* candidates on
abstain — stays with each caller, injected as ``rules`` and
``on_abstain``. The kernel removes the repeated loop-rules-then-escalate
boilerplate, not the domain.

The ``decided_by`` split is the same seam :data:`DecidedBy` names and
:data:`hpc_agent._wire.spawn_contract.DECISION_POINTS` enumerates; a
judgement :class:`Decision` carries exactly the :class:`Escalation`
block the worker reports back through ``WorkerDecision`` and the journal
persists as ``verdict_history``. Decision → escalation → recorded
verdict is one loop.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Generic, Protocol, TypeVar

from hpc_agent._wire.fixtures.escalation import CandidateAction, Escalation

if TYPE_CHECKING:
    from hpc_agent._wire.spawn_contract import DecidedBy

__all__ = ["Decision", "Rule", "AbstainHandler", "decide", "tally"]

E = TypeVar("E")

# A deterministic rule over an evidence vector: returns the action that
# resolves the decision, or ``None`` to abstain (fall through to the next
# rule, and finally to escalation). A rule that *could* match but should
# defer anyway — e.g. a fix already tried this episode — abstains too;
# the choice to escalate is just "no rule fired".
Rule = Callable[[Any], "CandidateAction | None"]

# Builds the escalation offered when every rule abstains — the candidate
# actions + reason a judgement point hands to the decision-maker. Takes the
# same evidence so the offer can be context-specific (the exhausted strategy,
# the ambiguous-class candidates, …).
AbstainHandler = Callable[[Any], "Escalation"]


@dataclass(frozen=True)
class Decision(Generic[E]):
    """The verdict at one decision point — the primordial object.

    ``decided_by="code"`` carries ``chosen`` (the branch the rules
    resolved to) and no escalation. ``decided_by="judgement"`` carries an
    :class:`Escalation` (candidates + evidence) and no ``chosen`` — the
    verdict picks one of the candidates downstream. The two are mutually
    exclusive, the same way :class:`hpc_agent.ops.recover.resolve.Resolution`
    splits today.
    """

    point: str
    decided_by: DecidedBy
    chosen: CandidateAction | None = None
    escalation: Escalation | None = None
    reason: str = ""

    @property
    def resolved(self) -> bool:
        """True when the deterministic rules decided it (no escalation)."""
        return self.decided_by == "code"


def decide(
    point: str,
    evidence: E,
    *,
    rules: Sequence[Callable[[E], CandidateAction | None]],
    on_abstain: Callable[[E], Escalation] | None = None,
    default: CandidateAction | None = None,
    reason_for: Callable[[CandidateAction], str] | None = None,
) -> Decision[E]:
    """Evaluate *rules* over *evidence*; resolve on the first hit, else fall back.

    First-match wins: rules are tried in order and the first to return a
    :class:`CandidateAction` resolves the point as ``decided_by="code"``. When
    every rule abstains (returns ``None``), the fallback is one of two shapes,
    and a point picks exactly one:

    * **escalate** — pass *on_abstain*; it builds the :class:`Escalation` and
      the point is ``decided_by="judgement"``. This is the resolvable-or-decide
      seam: a failure recovery, a judgement branch the deterministic layer
      cannot make.
    * **default** — pass a *default* :class:`CandidateAction`; the point stays
      ``decided_by="code"`` with that catch-all branch. This is a *total*
      deterministic ladder (e.g. campaign-advance's ``continue``,
      suggest-setup-action's ``fresh``) — a precedence list whose last branch
      always applies, so it never escalates.

    The code path's ``reason`` is ``reason_for(hit)`` when supplied, else the
    chosen candidate's own ``rationale``; the judgement path's ``reason`` is
    the escalation's. Passing neither *on_abstain* nor *default* is a misuse —
    rules abstained with no fallback configured — and raises ``ValueError``.
    """
    for rule in rules:
        hit = rule(evidence)
        if hit is not None:
            reason = reason_for(hit) if reason_for is not None else hit.rationale
            return Decision(point=point, decided_by="code", chosen=hit, reason=reason)
    if default is not None:
        reason = reason_for(default) if reason_for is not None else default.rationale
        return Decision(point=point, decided_by="code", chosen=default, reason=reason)
    if on_abstain is not None:
        escalation = on_abstain(evidence)
        return Decision(
            point=point,
            decided_by="judgement",
            escalation=escalation,
            reason=escalation.reason,
        )
    raise ValueError(
        f"decide({point!r}): all rules abstained but neither a default branch nor "
        "an on_abstain escalation handler was given"
    )


class _HasDecidedBy(Protocol):
    """Anything carrying the ``code``/``judgement`` verdict — :class:`Decision`
    and the domain views over it (e.g. ``recover.Resolution``) both qualify."""

    @property
    def decided_by(self) -> DecidedBy: ...


def tally(decisions: Iterable[_HasDecidedBy]) -> dict[str, int]:
    """Count code vs. judgement verdicts — the promotion health signal (#234).

    A point that repeatedly lands in ``judgement`` for the same evidence is
    the trigger to promote a deterministic rule for it (manually — no
    automatic codegen). Returns ``{"code": n, "judgement": m}``. Structurally
    typed so domain views (``Resolution``) tally alongside raw
    :class:`Decision` objects.
    """
    counts = {"code": 0, "judgement": 0}
    for decision in decisions:
        counts[decision.decided_by] += 1
    return counts
