"""``status-pipeline``: the deterministic status spine (wait + dispatch) in one call.

Folds ``worker_prompts/status.md`` Steps 2-4 — pick the wait surface, run it
to terminal/budget, branch on ``lifecycle_state`` — into ONE workflow
primitive. Those steps are mechanical: run ``monitor-flow``, then map its
``lifecycle_state`` to the next move. ``status-pipeline`` runs the branch in
code and returns a single typed ``stage_reached`` outcome, so the agent stops
hand-walking (and hand-branching) the lifecycle dispatch.

This is the control-flow-out-of-the-LLM pattern ``submit-pipeline`` applied to
the status workflow: where ``submit-pipeline`` absorbed the submit spine,
``status-pipeline`` absorbs the wait-then-dispatch spine.

Composition (``ops``-subject, so no cross-subject import):

    monitor-flow  →  (lifecycle_state dispatch)

Escalation-as-data (#231): only ``failed`` / ``abandoned`` set
``needs_decision=True`` — the caller still owns the failure classification +
resubmit judgement, surfaced here as ``last_status`` / ``failed_waves`` data.
``complete`` / ``timeout`` are clean terminals the agent just reports.

**Additive.** Does not replace the per-verb worker-prompt path — it is a new
verb the prompt may adopt. Nothing breaks if it is not yet wired in.

I/O contracts:

* Input: ``schemas/status_pipeline.input.json`` (from ``StatusPipelineSpec``).
* Output: ``schemas/status_pipeline.output.json`` (from ``StatusPipelineResult``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire._shared import LifecycleStateTerminal
from hpc_agent._wire.workflows.status_pipeline import (
    StatusPipelineResult,
    StatusPipelineSpec,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.ops.monitor_flow import monitor_flow

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["status_pipeline"]

_REASON: dict[str, str] = {
    "complete": "every task reported complete; proceed to the aggregate workflow.",
    "timeout": (
        "wall-clock budget elapsed with the cluster jobs still live; re-invoke "
        "to keep watching (no decision needed)."
    ),
    "failed": (
        "the run reached a terminal failed state; classify the failed tasks' "
        "stderr (recoverable vs not) and decide resubmit-failed / reconcile-journal."
    ),
    "abandoned": (
        "the recorded jobs are no longer known to the scheduler; run "
        "reconcile-journal to confirm before re-submitting."
    ),
}


@primitive(
    name="status-pipeline",
    verb="workflow",
    composes=["monitor-flow"],
    side_effects=[
        SideEffect("ssh", "<cluster> (status polls)"),
        SideEffect("writes-tick-log", "<experiment_dir>/<run_id>.monitor.jsonl"),
    ],
    error_codes=[
        errors.SpecInvalid,
        errors.SshUnreachable,
        errors.RemoteCommandFailed,
        errors.ClusterUnknown,
    ],
    idempotent=True,
    idempotency_key="status.monitor.run_id",
    cli=CliShape(
        help=(
            "Deterministic status spine in one call: monitor-flow (wait to "
            "terminal/budget) → lifecycle_state dispatch. Reports one typed "
            "stage_reached outcome; sets needs_decision only on failed / abandoned."
        ),
        spec_arg=True,
        spec_model=StatusPipelineSpec,
        experiment_dir_arg=True,
        requires_ssh=True,
        schema_ref=SchemaRef(input="status_pipeline"),
    ),
    agent_facing=True,
)
def status_pipeline(experiment_dir: Path, *, spec: StatusPipelineSpec) -> StatusPipelineResult:
    """Watch the run to terminal/budget, then map ``lifecycle_state`` to one outcome.

    Returns a single :class:`StatusPipelineResult`; ``stage_reached`` is the
    deterministic dispatch the caller branches on, and ``needs_decision`` is set
    only on ``failed`` / ``abandoned`` (the lifecycle outcomes that hand a
    genuine judgement back). ``complete`` / ``timeout`` are clean terminals.
    """
    result = monitor_flow(experiment_dir, spec=spec.monitor)
    # monitor-flow guarantees a terminal lifecycle. The wire model types it as
    # LifecycleStateTerminal, but pydantic field access widens to ``str`` under
    # mypy (no pydantic mypy plugin in this repo), so cast back to the Literal.
    lifecycle = cast(LifecycleStateTerminal, result.lifecycle_state)
    return StatusPipelineResult(
        stage_reached=lifecycle,
        needs_decision=lifecycle in {"failed", "abandoned"},
        reason=_REASON[lifecycle],
        run_id=result.run_id,
        lifecycle_state=lifecycle,
        last_status=result.last_status,
        combined_waves=list(result.combined_waves),
        failed_waves=list(result.failed_waves),
        ticks=result.ticks,
        elapsed_seconds=result.elapsed_seconds,
        escalation_reason=result.escalation_reason,
    )
