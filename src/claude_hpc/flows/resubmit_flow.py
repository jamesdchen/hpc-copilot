"""Resubmit pipeline — composes the same atoms ``submit_flow`` uses.

The original ``cmd_resubmit`` was a journal-only operation: it bumped
retry counters and stamped a request_id, but the actual cluster-side
qsub was the caller's job. That left two ways to put work on the
cluster — ``submit_flow`` (rsync + deploy + qsub + record) and the
slash command's hand-rolled "now run sbatch with the failed-id array
expression" dance — diverging on which survival atoms fire and how
overrides reach the scheduler.

:func:`resubmit_flow` is the macro that closes the loop. It mirrors
:func:`~claude_hpc.flows.submit_flow.submit_flow`'s shape —
frozen result dataclass, keyword-only args, raises typed errors — and
composes:

1. **Sidecar load** (single read, shared across the rest of the pipeline).
2. **Preempted detection** — raises :class:`~claude_hpc.errors.Preempted`
   when every failed task carries a preempt marker, before any
   cluster-side work.
3. **Survival planner** —
   :func:`~claude_hpc.planning.resubmit_planner.plan_resubmit_overrides`
   applies the same atoms ``plan_submit`` runs, so a cold-start retry
   gets the mem buffer + walltime arbitrage the initial submit would
   have applied.
4. **Queue-wait advisor** —
   :func:`~claude_hpc.forecast.resubmit_advisor.recommend_resubmit_window`
   surfaces an opt-out advisory of "submit now" vs "wait N hours" so
   the agent can throttle into a cheaper diurnal window.
5. **Cluster-side resubmission** (opt-in via ``submit_to_cluster=True``)
   — composes the *same* atoms submit_flow uses on the resubmit shape:
   :func:`~claude_hpc.planning.resubmit_batching.resubmit_plan`
   packs the failed IDs into compact array expressions, the scheduler
   backend (Slurm/SGE) submits each batch with the planner-adjusted
   overrides rendered as ``extra_flags``, and the resulting job IDs
   flow into the journal alongside the retry counters.
6. **Journal update** — :func:`runner.resubmit_failed` records the
   retry with the *planner-adjusted* overrides so monitor / aggregate
   downstream see the truth, not the raw 2× table. When the cluster-
   side step ran, the new job IDs land in the same call so the
   journal stays in sync.

``cmd_resubmit`` becomes a thin argparse → spec → flow adapter; future
callers (auto-retry from monitor_flow, programmatic resubmit from a
campaign driver) call this function directly without re-implementing
the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from claude_hpc import errors, runner
from claude_hpc._internal.lifecycle import FailureCategory
from claude_hpc.planning.resubmit_batching import resubmit_plan
from claude_hpc.planning.resubmit_planner import (
    PlannedResubmitOverrides,
    plan_resubmit_overrides,
)
from claude_hpc.state.runs import read_run_sidecar

if TYPE_CHECKING:
    import json as _json  # noqa: F401  # for type-checker symbol stability
    from pathlib import Path

    from claude_hpc.forecast.resubmit_advisor import ResubmitRecommendation
    from claude_hpc.infra.backends import HPCBackend

__all__ = [
    "ResubmitFlowResult",
    "render_overrides_to_extra_flags",
    "resubmit_flow",
]


_VALID_CATEGORIES = frozenset({fc.value for fc in FailureCategory})


@dataclass(frozen=True)
class ResubmitFlowResult:
    """Return shape of :func:`resubmit_flow`.

    ``planner`` is ``None`` only when the run sidecar is missing or
    its ``cluster``/``profile`` keys aren't strings — in that case
    overrides flow through unmodified and the caller still gets the
    journal update. ``forecast_recommendation`` is ``None`` whenever
    ``consult_forecast`` was disabled or the sidecar wasn't readable.
    ``cluster_submitted`` reports whether the macro actually ran the
    qsub step (opt-in via ``submit_to_cluster``); ``new_job_ids``
    carries the job IDs returned by the scheduler when it did, or an
    empty list otherwise.
    """

    run_id: str
    job_ids: list[str]
    retries: dict[str, dict[str, Any]]
    request_id: str
    deduped: bool
    planner: PlannedResubmitOverrides | None
    forecast_recommendation: ResubmitRecommendation | None
    cluster_submitted: bool = False
    new_job_ids: list[str] = field(default_factory=list)

    def to_envelope_data(self) -> dict[str, Any]:
        """Render to the shape ``cmd_resubmit`` emits as its envelope payload."""
        out: dict[str, Any] = {
            "run_id": self.run_id,
            "retries": self.retries,
            "job_ids": list(self.job_ids),
            "request_id": self.request_id,
            "deduped": self.deduped,
            "cluster_submitted": self.cluster_submitted,
        }
        if self.new_job_ids:
            out["new_job_ids"] = list(self.new_job_ids)
        if self.planner is not None:
            out["planner"] = self.planner.to_dict()
        if self.forecast_recommendation is not None:
            out["forecast_recommendation"] = self.forecast_recommendation.to_dict()
        return out


def render_overrides_to_extra_flags(
    scheduler: str,
    overrides: dict[str, Any] | None,
) -> list[str]:
    """Render the planner's override dict into scheduler-specific qsub flags.

    Maps the planner-adjusted (or caller-supplied) override keys to the
    flags the scheduler accepts on the qsub command line:

    * ``mem_mb`` — Slurm ``--mem=NM`` (suffix M); SGE ``-l h_data=NM``.
    * ``walltime_sec`` — Slurm ``--time=HH:MM:SS``; SGE ``-l h_rt=HH:MM:SS``.
    * ``gpus`` — Slurm ``--gpus=N``; SGE ``-l gpu=N``.
    * ``cpus`` — Slurm ``--cpus-per-task=N``; SGE ``-pe shared N``.

    Unknown keys are silently dropped — the planner emits documented
    keys only, and a typo'd override should not crash the whole
    resubmit. Unknown scheduler raises :class:`ValueError` (the
    backend lookup would fail anyway downstream).
    """
    if not overrides:
        return []
    s = (scheduler or "").lower()
    if s not in {"slurm", "sge", "sge_remote"}:
        raise ValueError(
            f"render_overrides_to_extra_flags: unknown scheduler {scheduler!r}; "
            "expected 'slurm' or 'sge'"
        )

    out: list[str] = []
    mem_mb = overrides.get("mem_mb")
    walltime_sec = overrides.get("walltime_sec")
    gpus = overrides.get("gpus")
    cpus = overrides.get("cpus")

    if s == "slurm":
        if isinstance(mem_mb, int) and mem_mb > 0:
            out += [f"--mem={mem_mb}M"]
        if isinstance(walltime_sec, int) and walltime_sec > 0:
            out += [f"--time={_format_walltime(walltime_sec)}"]
        if isinstance(gpus, int) and gpus > 0:
            out += [f"--gpus={gpus}"]
        if isinstance(cpus, int) and cpus > 0:
            out += [f"--cpus-per-task={cpus}"]
    else:  # sge / sge_remote
        if isinstance(mem_mb, int) and mem_mb > 0:
            out += ["-l", f"h_data={mem_mb}M"]
        if isinstance(walltime_sec, int) and walltime_sec > 0:
            out += ["-l", f"h_rt={_format_walltime(walltime_sec)}"]
        if isinstance(gpus, int) and gpus > 0:
            out += ["-l", f"gpu={gpus}"]
        if isinstance(cpus, int) and cpus > 0:
            out += ["-pe", "shared", str(cpus)]
    return out


def _format_walltime(walltime_sec: int) -> str:
    """Format seconds as ``HH:MM:SS`` for Slurm/SGE walltime flags."""
    h = walltime_sec // 3600
    m = (walltime_sec % 3600) // 60
    s = walltime_sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


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
    submit_to_cluster: bool = False,
    script: str | None = None,
    backend: str | None = None,
    job_name: str | None = None,
    job_env: dict[str, str] | None = None,
    constraints: Any = None,
    backend_factory: Any = None,
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

    cluster_submitted = False
    cluster_job_ids: list[str] = []
    if submit_to_cluster:
        if script is None or backend is None or job_name is None:
            raise errors.SpecInvalid(
                "submit_to_cluster=True requires script, backend, and job_name kwargs"
            )
        from claude_hpc._internal import session as _session

        existing = _session.load_run(experiment_dir, run_id)
        if existing is None:
            # No journal record means the post-submit `resubmit_failed`
            # bookkeeping would raise JournalCorrupt — and we'd already
            # have orphaned cluster jobs by that point. Fail up-front so
            # nothing is submitted that the framework can't track.
            raise errors.JournalCorrupt(
                f"resubmit_flow: no journal record for run_id={run_id!r}; "
                "cannot submit_to_cluster without a journal record to "
                "track the new jobs against."
            )
        derived_rid = request_id or runner.derive_resubmit_request_id(
            failed_task_ids=failed_task_ids,
            category=category,
            overrides=effective_overrides,
        )
        already_done = existing.last_resubmit_request_id == derived_rid
        if already_done:
            # Replaying the same request_id — earlier call already
            # submitted the batches. Surface the prior job_ids on the
            # cluster_job_ids field so callers branching on
            # cluster_submitted/cluster_job_ids see the durable state
            # instead of treating the dedup as "no submission happened".
            cluster_job_ids = list(existing.job_ids or [])
            cluster_submitted = True
        if not already_done:
            partial_ids: list[str] = []
            try:
                cluster_job_ids = _submit_resubmit_batches(
                    experiment_dir=experiment_dir,
                    run_id=run_id,
                    failed_task_ids=failed_task_ids,
                    effective_overrides=effective_overrides,
                    ssh_target=(sidecar or {}).get("ssh_target") or "",
                    remote_path=(sidecar or {}).get("remote_path") or "",
                    scheduler=backend,
                    script=script,
                    job_name=job_name,
                    job_env=dict(job_env or {}),
                    total_tasks=int(existing.total_tasks or 0),
                    constraints=constraints,
                    backend_factory=backend_factory,
                    submitted_ids_out=partial_ids,
                )
            except errors.RemoteCommandFailed:
                # Persist whatever batches succeeded BEFORE re-raising
                # so a retry doesn't double-submit batches 0..N-1.
                if partial_ids:
                    from claude_hpc._schema_models.actions.resubmit import (
                        ResubmitSpec as _ResubmitSpec,
                    )

                    runner.resubmit_failed(
                        experiment_dir,
                        run_id,
                        spec=_ResubmitSpec(
                            failed_task_ids=failed_task_ids,
                            category=category,  # type: ignore[arg-type]
                            overrides=effective_overrides,
                            new_job_ids=partial_ids,
                            request_id=request_id,
                        ),
                    )
                raise
            cluster_submitted = True

    from claude_hpc._schema_models.actions.resubmit import ResubmitSpec

    record, deduped, rid = runner.resubmit_failed(
        experiment_dir,
        run_id,
        spec=ResubmitSpec(
            failed_task_ids=failed_task_ids,
            category=category,  # type: ignore[arg-type]  # str → FailureCategoryResubmittable Literal validated at construction
            overrides=effective_overrides,
            new_job_ids=cluster_job_ids if cluster_submitted else new_job_ids,
            request_id=request_id,
        ),
    )

    return ResubmitFlowResult(
        run_id=record.run_id,
        job_ids=list(record.job_ids),
        retries=dict(record.retries),
        request_id=rid,
        deduped=deduped,
        planner=planner_result,
        forecast_recommendation=forecast_recommendation,
        cluster_submitted=cluster_submitted,
        new_job_ids=list(cluster_job_ids),
    )


def _submit_resubmit_batches(
    *,
    experiment_dir: Path,
    run_id: str,
    failed_task_ids: list[int],
    effective_overrides: dict[str, Any] | None,
    ssh_target: str,
    remote_path: str,
    scheduler: str,
    script: str,
    job_name: str,
    job_env: dict[str, str],
    total_tasks: int,
    constraints: Any,
    backend_factory: Any,
    submitted_ids_out: list[str] | None = None,
) -> list[str]:
    """Build the array-submission shape and qsub each batch.

    Composes the cluster-side atoms ``submit_flow`` uses on the shape
    appropriate for resubmits:

    * :func:`resubmit_plan` — packs failed IDs into compact array
      expressions ("3,7,12-14") within ``constraints.max_array_size``
      and ``constraints.max_concurrent_jobs``. Falls back to the
      stdlib defaults when ``constraints`` is None.
    * Backend ``_build_command`` + ``_execute_command`` — same private
      surface :func:`submit_flow._make_single_array_submission` uses,
      with ``extra_flags`` carrying the planner-adjusted overrides
      rendered as scheduler flags.

    *backend_factory* is an injection seam for tests — when ``None``
    the production :func:`~claude_hpc.flows.submit_flow._build_backend`
    constructs a real SSH-backed scheduler client. Tests pass a stub
    that records calls without touching a network.
    """
    from claude_hpc.planning.constraints import ClusterConstraints

    plan = resubmit_plan(
        task_count=max(total_tasks, max(failed_task_ids) + 1),
        failed_task_ids=failed_task_ids,
        overrides=effective_overrides,
        constraints=constraints if isinstance(constraints, ClusterConstraints) else None,
    )

    extra_flags = render_overrides_to_extra_flags(scheduler, effective_overrides)

    if backend_factory is None:
        from claude_hpc.flows.submit_flow import _build_backend

        backend_obj = _build_backend(
            backend_name=scheduler,
            script=script,
            ssh_target=ssh_target,
            remote_path=remote_path,
            pass_env_keys=tuple(job_env.keys()),
            job_env_keys=tuple(job_env.keys()),
        )
    else:
        backend_obj = backend_factory(
            scheduler=scheduler,
            script=script,
            ssh_target=ssh_target,
            remote_path=remote_path,
            job_env_keys=tuple(job_env.keys()),
        )

    # Share the list with the caller so a mid-loop failure still
    # surfaces the IDs that DID land on the cluster.
    submitted_ids: list[str] = submitted_ids_out if submitted_ids_out is not None else []
    cwd = experiment_dir
    for batch in plan.batches:
        job_id = _submit_one_batch(
            backend_obj,
            job_name=job_name,
            task_range=batch.task_range,
            job_env=job_env,
            extra_flags=extra_flags,
            cwd=cwd,
        )
        submitted_ids.append(job_id)
    return submitted_ids


def _submit_one_batch(
    backend: HPCBackend,
    *,
    job_name: str,
    task_range: str,
    job_env: dict[str, str],
    extra_flags: list[str],
    cwd: Path,
) -> str:
    """Submit one batch with a precomputed array expression. Returns the job id.

    Mirrors :func:`~claude_hpc.flows.submit_flow._make_single_array_submission`
    but accepts an arbitrary ``task_range`` (e.g., ``"3,7,12-14"``)
    instead of hardcoding ``"1-N"``, and threads ``extra_flags`` so the
    planner-adjusted overrides land on the qsub command line.
    """
    backend._setup_log_dir()  # type: ignore[attr-defined]
    cmd = backend._build_command(  # type: ignore[attr-defined]
        task_range, job_name, job_env, extra_flags=extra_flags
    )
    result = backend._execute_command(cmd, job_env, cwd)  # type: ignore[attr-defined]
    if result.returncode != 0:
        stderr_msg = result.stderr.strip() if result.stderr else "(no stderr)"
        raise errors.RemoteCommandFailed(
            f"resubmit failed (exit {result.returncode}) for array {task_range}: {stderr_msg}"
        )
    match = backend.JOB_ID_REGEX.search(result.stdout)
    if not match:
        raise errors.RemoteCommandFailed(
            f"could not parse job id from scheduler output: {result.stdout!r}"
        )
    return match.group(1)


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


def _raise_if_all_preempted(sidecar: dict, failed_task_ids: list[int]) -> None:
    tasks_block = sidecar.get("tasks") or {}
    ids_int = [int(t) for t in failed_task_ids]
    all_preempted = bool(ids_int) and all(
        isinstance(tasks_block.get(str(tid)), dict) and "preempt" in tasks_block.get(str(tid), {})
        for tid in ids_int
    )
    if all_preempted:
        raise errors.Preempted(
            f"all {len(ids_int)} task ids in resubmit spec carry "
            "preempt markers; the campus user got bumped by higher-priority "
            "work, not failed. Resubmit when scheduler pressure abates."
        )
