"""hpc_agent._kernel.decision — the primordial decision object.

One evaluator every router atom calls: ``decide`` runs ordered
deterministic rules over an evidence vector and returns a uniform
:class:`Decision` — resolved (``decided_by="code"``) or escalated
(``decided_by="judgement"``). ``tally`` is the code-vs-judgement
promotion signal. See :mod:`hpc_agent._kernel.decision.kernel`.
"""

from hpc_agent._kernel.decision.kernel import (
    AbstainHandler,
    Decision,
    Rule,
    decide,
    tally,
)

__all__ = ["AbstainHandler", "Decision", "Rule", "decide", "tally"]
