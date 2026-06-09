"""Headless campaign driver — the campaign configuration of the neutral loop.

The generic tick-loop now lives in
:mod:`hpc_agent._kernel.lifecycle.drive` — neutral substrate that advances
one ``delegate`` step per invocation and knows nothing about campaigns. This
module is the campaign *caller* that configures it: it supplies the campaign
step map (``monitor`` / ``aggregate``) and the default ``claude -p`` judgement
resolver, then exposes the ``hpc-campaign-driver`` console-script entry point.

This is deliberately **not** a ``@primitive``. Primitives are pure JSON-in /
JSON-out tools that an agent invokes; the loop does the opposite — it
*drives*, and for judgement steps it may spawn an LLM (``claude -p``), only
behind the explicit ``--allow-agent-steps`` opt-in. One step per invocation:
idempotent and cron-friendly. Wrap it in cron or ``/loop`` to walk a campaign;
the on-disk state (run sidecars, journal, cursors) is the only thing carried
between ticks.

``plan_action`` is re-exported from ``drive`` so existing importers and tests
keep their import path unchanged.

Usage::

    python -m hpc_agent.meta.campaign.driver --experiment-dir .
    python -m hpc_agent.meta.campaign.driver --experiment-dir . --dry-run
    python -m hpc_agent.meta.campaign.driver --experiment-dir . --allow-agent-steps
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from hpc_agent._kernel.lifecycle.drive import (
    JudgementResolver,
    StepTable,
    default_judgement_resolver,
    drive,
    load_context,
    plan_action,
)

__all__ = [
    "StepTable",
    "JudgementResolver",
    "CampaignLoopConfig",
    "load_context",
    "plan_action",
    "default_judgement_resolver",
    "main",
]

# Campaign's deterministic steps. The only campaign-flavored content the loop
# needs — kept here, in the campaign module, and handed to the neutral
# mechanism in ``drive`` via ``CampaignLoopConfig``. Wrapped in a
# ``MappingProxyType`` so the frozen config's default is genuinely immutable:
# a caller can't mutate ``config.step_table`` and silently pollute this shared
# module global for every later config.
_CAMPAIGN_STEP_VERB: Mapping[str, str] = MappingProxyType(
    {
        "monitor": "monitor-flow",
        "aggregate": "aggregate-flow",
    }
)


@dataclass(frozen=True)
class CampaignLoopConfig:
    """The campaign-flavored configuration the neutral loop runs under.

    Bundles the two seams the loop injects — *step_table* (which deterministic
    verb each ``delegate.step`` maps to) and *resolver* (how a judgement step
    is executed). The defaults reproduce today's ``hpc-campaign-driver``
    behavior exactly: the monitor/aggregate map and the ``run_workflow``-backed
    ``claude -p`` resolver.
    """

    step_table: StepTable = field(default_factory=lambda: _CAMPAIGN_STEP_VERB)
    resolver: JudgementResolver = default_judgement_resolver


def main(argv: list[str] | None = None, *, config: CampaignLoopConfig | None = None) -> int:
    """Advance one campaign workflow step. Returns a process exit code."""
    if config is None:
        config = CampaignLoopConfig()
    return drive(
        argv,
        step_table=config.step_table,
        resolver=config.resolver,
        prog="hpc-campaign-driver",
        description="Advance one campaign workflow step from load-context's delegate block.",
    )


if __name__ == "__main__":
    sys.exit(main())
