"""Resubmit pipeline — composes preempted-detection, planner, advisor, journal.

The original ``cmd_resubmit`` chained four concerns inline: read the run
sidecar, raise :class:`~claude_hpc.errors.Preempted` on all-preempted
batches, apply the queue-wait advisor, and bump retry counters via
:func:`runner.resubmit_failed`. As the survival atoms (``walltime_arbitrage``,
``cold_start_mem_buffer``, ``should_daisy_chain``) joined the picture
the inline shape stopped scaling — and worse, those atoms only fire on
*initial* submit, never on resubmit. The static 2× memory / 4× walltime
table in ``/monitor-hpc`` rode the retries straight past the survival
machinery the planner uses at submit time.

:func:`resubmit_flow` is the macro fix. It mirrors
:func:`~claude_hpc.orchestrator.submit_flow.submit_flow`'s shape — frozen
result dataclass, keyword-only args, raises typed errors — and composes:

1. **Sidecar load** (single read, shared across the rest of the pipeline).
2. **Preempted detection** — raises :class:`~claude_hpc.errors.Preempted`
   when every failed task carries a preempt marker, before any
   cluster-side work.
3. **Survival planner** —
   :func:`~claude_hpc.orchestrator.resubmit_planner.plan_resubmit_overrides`
   applies the same atoms ``plan_submit`` runs, so a cold-start retry
   gets the mem buffer + walltime arbitrage the initial submit would
   have applied.
4. **Queue-wait advisor** —
   :func:`~claude_hpc.forecast.resubmit_advisor.recommend_resubmit_window`
   surfaces an opt-out advisory of "submit now" vs "wait N hours" so
   the agent can throttle into a cheaper diurnal window.
5. **Journal update** — :func:`runner.resubmit_failed` records the
   retry with the *planner-adjusted* overrides so monitor / aggregate
   downstream see the truth, not the raw 2× table.

``cmd_resubmit`` becomes a thin argparse → spec → flow adapter; future
callers (auto-retry from monitor_flow, programmatic resubmit from a
campaign driver) call this function directly without re-implementing
the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from claude_hpc import errors
from claude_hpc._internal.lifecycle import FailureCategory
from claude_hpc.orchestrator import runner
from claude_hpc.orchestrator.resubmit_planner import (
    PlannedResubmitOverrides,
    plan_resubmit_overrides,
)
from claude_hpc.orchestrator.runs import read_run_sidecar

if TYPE_CHECKING:
    import json as _json  # noqa: F401  # for type-checker symbol stability
    from pathlib import Path

    from claude_hpc.forecast.resubmit_advisor import ResubmitRecommendation

__all__ = ["ResubmitFlowResult", "resubmit_flow"]


_VALID_CATEGORIES = frozenset({fc.value for fc in FailureCategory})


@dataclass(frozen=True)
class ResubmitFlowResult:
    """Return shape of :func:`resubmit_flow`.

    ``planner`` is ``None`` only when the run sidecar is missing or
    its ``cluster``/``profile`` keys aren't strings — in that case
    overrides flow through unmodified and the caller still gets the
    journal update. ``forecast_recommendation`` is ``None`` whenever
    ``consult_forecast`` was disabled or the sidecar wasn't readable.
    """

    run_id: str
    job_ids: list[str]
    retries: dict[str, dict[str, Any]]
    request_id: str
    deduped: bool
    planner: PlannedResubmitOverrides | None
    forecast_recommendation: ResubmitRecommendation | None

    def to_envelope_data(self) -> dict[str, Any]:
        """Render to the shape ``cmd_resubmit`` emits as its envelope payload."""
        out: dict[str, Any] = {
            "run_id": self.run_id,
            "retries": self.retries,
            "job_ids": list(self.job_ids),
            "request_id": self.request_id,
            "deduped": self.deduped,
        }
        if self.planner is not None:
            out["planner"] = self.planner.to_dict()
        if self.forecast_recommendation is not None:
            out["forecast_recommendation"] = self.forecast_recommendation.to_dict()
        return out


def resubmit_flow(
    experiment_dir: Path,
    run_id: str,
    *,
    failed_task_ids: list[int],
    category: str,
    overrides: dict[str, Any] | None = None,
    new_job_ids: list[str] | None = None,
    request_id: str | None = None,
    consult_forecast: bool = True,
    forecast_within_hours: int = 24,
) -> ResubmitFlowResult:
    """Execute the resubmit pipeline and emit a single result.

    Errors raise the existing :class:`~claude_hpc.errors.HpcError`
    hierarchy so the CLI adapter can surface them as typed envelope
    errors uniformly with ``submit_flow``.

    Raises
    ------
    errors.SpecInvalid
        If *failed_task_ids* is empty or *category* is not in the
        canonical :class:`FailureCategory` set.
    errors.Preempted
        If *category* is ``"preempted"`` and every task in
        *failed_task_ids* carries a per-task ``preempt`` marker
        (set by ``dispatch.py``'s SIGTERM handler) — the campus user
        was bumped, not failed; the caller should throttle.
    errors.JournalCorrupt
        If no run record exists for *run_id*.
    """
    if not failed_task_ids:
        raise errors.SpecInvalid("failed_task_ids must be non-empty")
    if category not in _VALID_CATEGORIES:
        raise errors.SpecInvalid(
            f"category must be one of {sorted(_VALID_CATEGORIES)}; got {category!r}"
        )

    sidecar = _safe_read_sidecar(experiment_dir, run_id)

    if category == "preempted" and sidecar is not None:
        _raise_if_all_preempted(sidecar, failed_task_ids)

    cluster, profile = _extract_cluster_profile(sidecar)

    planner_result: PlannedResubmitOverrides | None = None
    effective_overrides = overrides
    if cluster is not None and profile is not None:
        planner_result = plan_resubmit_overrides(
            experiment_dir,
            profile=profile,
            cluster=cluster,
            base_overrides=overrides,
        )
        effective_overrides = planner_result.overrides

    forecast_recommendation: ResubmitRecommendation | None = None
    if consult_forecast and cluster is not None and profile is not None:
        from claude_hpc.forecast.resubmit_advisor import recommend_resubmit_window

        forecast_recommendation = recommend_resubmit_window(
            experiment_dir,
            profile=profile,
            cluster=cluster,
            within_hours=forecast_within_hours,
        )

    record, deduped, rid = runner.resubmit_failed(
        experiment_dir,
        run_id,
        failed_task_ids=failed_task_ids,
        category=category,
        overrides=effective_overrides,
        new_job_ids=new_job_ids,
        request_id=request_id,
    )

    return ResubmitFlowResult(
        run_id=record.run_id,
        job_ids=list(record.job_ids),
        retries=dict(record.retries),
        request_id=rid,
        deduped=deduped,
        planner=planner_result,
        forecast_recommendation=forecast_recommendation,
    )


def _safe_read_sidecar(experiment_dir: Path, run_id: str) -> dict | None:
    import json

    try:
        return read_run_sidecar(experiment_dir, run_id)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def _extract_cluster_profile(
    sidecar: dict | None,
) -> tuple[str | None, str | None]:
    if sidecar is None:
        return None, None
    cluster = sidecar.get("cluster")
    profile = sidecar.get("profile")
    return (
        cluster if isinstance(cluster, str) else None,
        profile if isinstance(profile, str) else None,
    )


def _raise_if_all_preempted(
    sidecar: dict, failed_task_ids: list[int]
) -> None:
    tasks_block = sidecar.get("tasks") or {}
    ids_int = [int(t) for t in failed_task_ids]
    all_preempted = bool(ids_int) and all(
        isinstance(tasks_block.get(str(tid)), dict)
        and "preempt" in tasks_block.get(str(tid), {})
        for tid in ids_int
    )
    if all_preempted:
        raise errors.Preempted(
            f"all {len(ids_int)} task ids in resubmit spec carry "
            "preempt markers; the campus user got bumped by higher-priority "
            "work, not failed. Resubmit when scheduler pressure abates."
        )
