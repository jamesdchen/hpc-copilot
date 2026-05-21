"""Resubmit pipeline — composes the same atoms ``submit_flow`` uses.

The original ``cmd_resubmit`` was a journal-only operation: it bumped
retry counters and stamped a request_id, but the actual cluster-side
qsub was the caller's job. That left two ways to put work on the
cluster — ``submit_flow`` (rsync + deploy + qsub + record) and the
slash command's hand-rolled "now run sbatch with the failed-id array
expression" dance — diverging on which survival atoms fire and how
overrides reach the scheduler.

:func:`resubmit_flow` is the macro that closes the loop. It mirrors
:func:`~hpc_agent.flows.submit_flow.submit_flow`'s shape —
frozen result dataclass, keyword-only args, raises typed errors — and
composes:

1. **Sidecar load** (single read, shared across the rest of the pipeline).
2. **Preempted detection** — raises :class:`~hpc_agent.errors.Preempted`
   when every failed task carries a preempt marker, before any
   cluster-side work.
3. **Cluster-side resubmission** (opt-in via ``submit_to_cluster=True``)
   — composes the *same* atoms submit_flow uses on the resubmit shape:
   :func:`~hpc_agent.planning.resubmit_batching.resubmit_plan`
   packs the failed IDs into compact array expressions, the scheduler
   backend (Slurm/SGE) submits each batch with the caller-supplied
   overrides rendered as ``extra_flags``, and the resulting job IDs
   flow into the journal alongside the retry counters.
4. **Journal update** — :func:`runner.resubmit_failed` records the
   retry with the caller-supplied overrides so monitor / aggregate
   downstream see the truth. When the cluster-side step ran, the new
   job IDs land in the same call so the journal stays in sync.

Resource overrides are applied verbatim as the caller passes them —
``resubmit_flow`` does no automatic right-sizing of its own.

``cmd_resubmit`` becomes a thin argparse → spec → flow adapter; future
callers (auto-retry from monitor_flow, programmatic resubmit from a
campaign driver) call this function directly without re-implementing
the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from hpc_agent import errors, runner
from hpc_agent._internal.lifecycle import FailureCategory  # noqa: F401 — re-export
from hpc_agent._schema_models._shared import FailureCategoryResubmittable
from hpc_agent.planning.resubmit_batching import resubmit_plan
from hpc_agent.state.runs import read_run_sidecar

if TYPE_CHECKING:
    import json as _json  # noqa: F401  # for type-checker symbol stability
    from pathlib import Path

    from hpc_agent.infra.backends import HPCBackend

__all__ = [
    "ResubmitFlowResult",
    "render_overrides_to_extra_flags",
    "resubmit_flow",
]


# Tighter than the full ``FailureCategory`` StrEnum — only categories the
# scheduler can resubmit get past this gate. Otherwise the up-front
# validation accepts a classifier-emitted code (e.g. ``import_error``)
# that ``ResubmitSpec`` (which is keyed on the narrower
# :class:`FailureCategoryResubmittable` Literal) rejects later, AFTER
# the cluster qsub already fired — orphaning the new jobs (v3 BUG-4V3-1).
from typing import get_args as _typing_get_args

_VALID_CATEGORIES = frozenset(_typing_get_args(FailureCategoryResubmittable))


@dataclass(frozen=True)
class ResubmitFlowResult:
    """Return shape of :func:`resubmit_flow`.

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
    if s not in {"slurm", "sge"}:
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
    else:  # sge
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
    submit_to_cluster: bool = False,
    script: str | None = None,
    backend: str | None = None,
    job_name: str | None = None,
    job_env: dict[str, str] | None = None,
    constraints: Any = None,
    backend_factory: Any = None,
) -> ResubmitFlowResult:
    """Execute the resubmit pipeline and emit a single result.

    Errors raise the existing :class:`~hpc_agent.errors.HpcError`
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

    effective_overrides = overrides

    cluster_submitted = False
    cluster_job_ids: list[str] = []
    if submit_to_cluster:
        if script is None or backend is None or job_name is None:
            raise errors.SpecInvalid(
                "submit_to_cluster=True requires script, backend, and job_name kwargs"
            )
        from hpc_agent._internal import session as _session

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
        # Match the dedup-depth the runner enforces: ``resubmit_failed``
        # checks BOTH ``last_resubmit_request_id`` AND the bounded
        # ``recent_resubmit_request_ids`` list (so an A→B→A replay
        # dedups correctly). If the flow only checks the last id, A→B→A
        # short-circuits the runner update but the cluster-submit step
        # below fires again and orphans the new array. Mirror the
        # runner's check here.
        _recent = list(existing.recent_resubmit_request_ids or [])
        # A prior call to *this* request failed mid-batch and left a
        # resume marker. Resuming takes precedence over the dedup check:
        # the request is NOT done, and the marker tells us which batches
        # already landed so we continue rather than re-submit or skip.
        pending = dict(existing.pending_resubmit or {})
        resuming = bool(pending) and pending.get("request_id") == derived_rid
        already_done = not resuming and (
            existing.last_resubmit_request_id == derived_rid or derived_rid in _recent
        )
        if already_done:
            # Replaying a completed request_id — earlier call already
            # submitted every batch. Surface the prior job_ids on the
            # cluster_job_ids field so callers branching on
            # cluster_submitted/cluster_job_ids see the durable state
            # instead of treating the dedup as "no submission happened".
            cluster_job_ids = list(existing.job_ids or [])
            cluster_submitted = True
        if not already_done:
            # Batches that landed on a prior partial attempt — one job
            # id per batch, in plan order — so we resume from batch
            # ``len(prior_ids)`` and keep the ids already collected.
            prior_ids = list(pending.get("job_ids", [])) if resuming else []
            partial_ids: list[str] = list(prior_ids)
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
                    start_batch=len(prior_ids),
                )
            except errors.RemoteCommandFailed:
                # Record progress so a retry resumes from the next
                # un-submitted batch — without this marker the retry
                # either re-submits the batches that already landed or
                # (if the request_id were stamped) skips the remainder.
                from hpc_agent._internal import session as _session_mod

                _session_mod.update_run_status(
                    experiment_dir,
                    run_id,
                    job_ids=list(partial_ids),
                    pending_resubmit={
                        "request_id": derived_rid,
                        "job_ids": list(partial_ids),
                    },
                )
                raise
            cluster_submitted = True
            # Whole plan landed — drop the resume marker so a later
            # replay of this request dedups instead of resuming.
            if pending:
                from hpc_agent._internal import session as _session_mod

                _session_mod.update_run_status(
                    experiment_dir, run_id, pending_resubmit={}
                )

    from hpc_agent._schema_models.actions.resubmit import ResubmitSpec

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
    start_batch: int = 0,
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
    the production :func:`~hpc_agent.flows.submit_flow._build_backend`
    constructs a real SSH-backed scheduler client. Tests pass a stub
    that records calls without touching a network.
    """
    from hpc_agent.planning.constraints import ClusterConstraints

    # When the caller didn't pass constraints, load them from the
    # sidecar's cluster's yaml entry. Symmetric with submit_flow, which
    # always threads cluster-specific constraints; without this fallback
    # the planner used stdlib defaults (max_array_size=1000) and
    # over-packed batches on clusters with stricter limits, tripping the
    # scheduler's batch-size guard at qsub time. We re-read the sidecar
    # here (already loaded once upstream) to keep the helper's signature
    # narrow — the IO cost is a single JSON read.
    effective_constraints: ClusterConstraints | None = (
        constraints if isinstance(constraints, ClusterConstraints) else None
    )
    if effective_constraints is None:
        try:
            from hpc_agent.infra.clusters import load_clusters_config, load_constraints
            from hpc_agent.state.runs import read_run_sidecar

            sidecar = read_run_sidecar(experiment_dir, run_id)
            cluster_name = sidecar.get("cluster") if isinstance(sidecar, dict) else None
            if cluster_name:
                clusters = load_clusters_config()
                cluster_cfg = clusters.get(cluster_name) if isinstance(clusters, dict) else None
                if isinstance(cluster_cfg, dict):
                    effective_constraints = load_constraints(cluster_cfg)
        except Exception:  # noqa: BLE001 — fall back to defaults on any failure
            effective_constraints = None

    plan = resubmit_plan(
        task_count=total_tasks,
        failed_task_ids=failed_task_ids,
        overrides=effective_overrides,
        constraints=effective_constraints,
    )

    extra_flags = render_overrides_to_extra_flags(scheduler, effective_overrides)

    if backend_factory is None:
        from hpc_agent.flows.submit_flow import _build_backend, _validate_ssh_target

        # Validate ssh_target up front — _build_backend no longer
        # double-validates internally (see BUG-4-10), so callers own the
        # check. We surface the same SpecInvalid envelope the submit
        # path uses.
        _validate_ssh_target(ssh_target)

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
    # surfaces the IDs that DID land on the cluster. *start_batch* skips
    # batches a prior partial attempt already submitted — ``resubmit_plan``
    # is deterministic, so plan.batches is the same across calls and
    # batch index N always denotes the same task range.
    submitted_ids: list[str] = submitted_ids_out if submitted_ids_out is not None else []
    cwd = experiment_dir
    for batch in plan.batches[start_batch:]:
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

    Mirrors :func:`~hpc_agent.flows.submit_flow._make_single_array_submission`
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
    """Return the sidecar dict, or ``None`` when the file is absent.

    Distinguishes ``FileNotFoundError`` (a missing sidecar is a benign
    pre-condition for callers that gate optional behaviour on it) from
    ``JSONDecodeError`` (corruption is a real failure that must NOT
    silently disable downstream gates — e.g. the preempt-throttle
    check would otherwise fire resubmits into the same storm that
    just corrupted the sidecar — v3 BUG-4V3-4). On corruption we log a
    warning and re-raise so the caller can decide whether to fail
    loud or bypass via an explicit flag.
    """
    import json

    try:
        return read_run_sidecar(experiment_dir, run_id)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        import logging

        logging.getLogger(__name__).warning(
            "sidecar for run_id=%s is corrupted; refusing to silently bypass downstream gates",
            run_id,
        )
        raise
    except OSError:
        return None


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
