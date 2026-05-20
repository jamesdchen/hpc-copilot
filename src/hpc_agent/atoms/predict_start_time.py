"""``predict-start-time`` primitive — agent-facing wrapper.

Pure local primitive. The slash command runs ``squeue`` + ``sshare``
over SSH and hands the raw text to this primitive; the primitive
parses + simulates + (optionally) calls a LightGBM residual model
+ returns the best-submit-offset recommendation.

Keeps SSH side effects at the slash-command boundary so the
framework primitive stays side-effect-free.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from hpc_agent import errors
from hpc_agent._internal.primitive import primitive
from hpc_agent._schema_models.queries.predict_start_time import (
    PredictStartTimeResult,
    PredictStartTimeSpec,
)

if TYPE_CHECKING:
    pass


@primitive(
    name="predict-start-time",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    cli="hpc-agent predict-start-time --spec <path>",
    agent_facing=True,
)
def predict_start_time_primitive(
    experiment_dir: Path,
    *,
    spec: PredictStartTimeSpec,
) -> PredictStartTimeResult:
    """Run the floor + residual predictor over candidate submit offsets.

    Returns the best (offset, predicted_start, total_time) and the
    full sweep for transparency.
    """
    from hpc_agent.forecast.predict_start import recommend_best_submit_time
    from hpc_agent.forecast.squeue_priority_field import parse_squeue_priority_field
    from hpc_agent.forecast.sshare_parser import parse_sshare

    queue = parse_squeue_priority_field(spec.squeue_text)
    fairshare = parse_sshare(spec.sshare_text or "") or None

    try:
        best = recommend_best_submit_time(
            experiment_dir,
            now_iso=spec.now_iso,
            queue=queue,
            partition=spec.partition,
            partition_slot_count=spec.partition_slot_count,
            your_priority=spec.your_priority,
            your_walltime_sec=spec.your_walltime_sec,
            pending_walltime_default_sec=spec.pending_walltime_default_sec,
            candidate_offsets_hours=tuple(spec.candidate_offsets_hours),
            your_user=spec.your_user,
            your_constraint=spec.your_constraint,
            fairshare_by_user=fairshare,
            model_path=Path(spec.model_path) if spec.model_path else None,
        )
    except ValueError as exc:
        raise errors.SpecInvalid(str(exc)) from exc

    from hpc_agent._schema_models.queries.predict_start_time import _CandidateOut

    return PredictStartTimeResult(
        best_submit_offset_hours=best.best_submit_offset_hours,
        best_predicted_start_iso=best.best_predicted_start_iso,
        best_total_time_sec=best.best_total_time_sec,
        candidates=[
            _CandidateOut(
                offset_hours=c.offset_hours,
                predicted_iso=c.forecast.predicted_iso,
                floor_pessimistic_iso=c.forecast.floor_pessimistic_iso,
                floor_optimistic_iso=c.forecast.floor_optimistic_iso,
                overhead_sec=c.forecast.overhead_sec,
                total_time_sec=c.total_time_sec,
                method=c.forecast.method,
            )
            for c in best.candidates
        ],
    )
