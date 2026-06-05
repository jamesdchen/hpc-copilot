"""Pydantic models for the ``status-pipeline`` workflow primitive.

The deterministic status *spine* as ONE call — the
control-flow-out-of-the-LLM move applied to ``worker_prompts/status.md``
Steps 2-4 (the wait-until-terminal surface + the ``lifecycle_state``
dispatch). That branch is mechanical: run ``monitor-flow`` to terminal/budget,
then map ``lifecycle_state`` to "proceed / re-watch / decide". ``status-pipeline``
runs that branch in code and reports a single typed ``stage_reached`` outcome,
so the agent stops hand-walking (and hand-branching) the lifecycle.

Composition (all ``ops``-subject, so no cross-subject import):

    monitor-flow  →  (lifecycle_state dispatch)

Scope: the **blocking / wait-until-terminal** surface — the canonical
campaign-loop case the driver sets via ``blocking: true``. The one-shot
``poll-run-status`` snapshot stays a direct verb (it has no branch to fold —
the caller decides its own cadence). The genuine judgement that follows a
``failed`` run — read the failed-task stderr, classify recoverable-vs-not,
then resubmit-failed — stays UPSTREAM as an escalation: ``status-pipeline``
flags it with ``needs_decision=True`` and hands back ``last_status`` /
``failed_waves`` as data, but does not itself resubmit.

Escalation-as-data (#231): only ``failed`` / ``abandoned`` set
``needs_decision=True``. ``complete`` is a clean terminal the caller just
proceeds from (to aggregation); ``timeout`` means the budget elapsed with the
jobs still live — the caller re-invokes to keep watching, no decision needed.

**Additive.** Does not replace the per-verb worker-prompt path — it is a new
verb the prompt may adopt. Nothing breaks if it is not yet wired in.

I/O contracts:

* Input: ``schemas/status_pipeline.input.json`` (from ``StatusPipelineSpec``).
* Output: ``schemas/status_pipeline.output.json`` (from ``StatusPipelineResult``).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import (
    CombinedWaves,
    FailedWaves,
    LifecycleStateTerminal,
    RunIdStrict,
)
from hpc_agent._wire.workflows.monitor_flow import MonitorFlowSpec


class StatusPipelineSpec(BaseModel):
    """Spec passed to ``hpc-agent status-pipeline --spec <file>``."""

    model_config = ConfigDict(extra="forbid", title="status-pipeline input spec")

    monitor: MonitorFlowSpec = Field(
        description=(
            "The wait-until-terminal monitor spec (run_id + poll cadence + "
            "wall-clock budget). status-pipeline runs it to terminal/budget and "
            "maps the resulting lifecycle_state to one typed stage_reached."
        ),
    )


class StatusPipelineResult(BaseModel):
    """Shape of the ``data`` field on a ``status-pipeline`` envelope.

    ``stage_reached`` is the deterministic dispatch the agent used to walk by
    hand. ``needs_decision`` flags the lifecycle outcomes that require a caller
    decision (failed / abandoned) — the decision-as-data shape (#231): the
    pipeline ran the lifecycle branch; only the genuine judgement (classify the
    failure, decide resubmit) is handed back.
    """

    model_config = ConfigDict(extra="forbid", title="status-pipeline output data")

    stage_reached: LifecycleStateTerminal = Field(
        description="Which lifecycle the monitored run reached.",
    )
    needs_decision: bool = Field(
        description=(
            "True for failed / abandoned (the caller classifies + decides "
            "resubmit/reconcile); False for the clean terminals (complete / timeout)."
        ),
    )
    reason: str = Field(description="Human-readable summary of the outcome / what must be decided.")
    run_id: RunIdStrict
    lifecycle_state: LifecycleStateTerminal = Field(
        description="The terminal-or-budget lifecycle_state monitor-flow returned.",
    )
    last_status: dict[str, Any] = Field(
        description="Final per-task status snapshot from the cluster-side reporter.",
    )
    combined_waves: CombinedWaves = Field(default_factory=list)
    failed_waves: FailedWaves = Field(default_factory=list)
    ticks: int | None = Field(
        default=None,
        description="Poll iterations monitor-flow executed before terminal/budget.",
    )
    elapsed_seconds: float | None = Field(
        default=None,
        description="Wall-clock seconds monitor-flow spent watching.",
    )
    escalation_reason: str | None = Field(
        default=None,
        description=(
            "monitor-flow's escalation_reason when it stopped short of 'complete' "
            "(e.g. failed_tasks_no_auto_recover_in_mvp); null on a clean complete."
        ),
    )
