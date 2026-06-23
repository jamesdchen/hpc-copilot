"""Resubmit pipeline — composes the same atoms ``submit_flow`` uses.

The original ``cmd_resubmit`` was a journal-only operation: it bumped
retry counters and stamped a request_id, but the actual cluster-side
qsub was the caller's job. That left two ways to put work on the
cluster — ``submit_flow`` (rsync + deploy + qsub + record) and the
slash command's hand-rolled "now run sbatch with the failed-id array
expression" dance — diverging on which survival atoms fire and how
overrides reach the scheduler.

:func:`resubmit_flow` is the macro that closes the loop. It mirrors
:func:`~hpc_agent.ops.submit_flow.submit_flow`'s shape —
frozen result dataclass, keyword-only args, raises typed errors — and
composes:

1. **Sidecar load** (single read, shared across the rest of the pipeline).
2. **Preempted detection** — raises :class:`~hpc_agent.errors.Preempted`
   when every failed task carries a preempt marker, before any
   cluster-side work.
3. **Cluster-side resubmission** (opt-in via ``submit_to_cluster=True``)
   — composes the *same* atoms submit_flow uses on the resubmit shape:
   :func:`~hpc_agent.ops.recover.batching.resubmit_plan`
   packs the failed IDs into compact array expressions, the scheduler
   backend (Slurm/SGE) submits each batch with the caller-supplied
   overrides rendered as ``extra_flags``, and the resulting job IDs
   flow into the journal alongside the retry counters.
4. **Journal update** — :func:`resubmit_failed` records the
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

import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.contract.vocabulary import FailureCategory  # noqa: F401 — re-export
from hpc_agent._wire._shared import FailureCategoryResubmittable
from hpc_agent.infra.resource_format import walltime_hms
from hpc_agent.ops.recover.batching import resubmit_plan
from hpc_agent.ops.recover.runner import derive_resubmit_request_id, resubmit_failed
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

    * ``mem_mb`` — Slurm ``--mem=NM``; SGE ``-l h_data=NM``; PBS Pro
      ``select=…:mem=Nmb``; TORQUE ``-l …,mem=Nmb``.
    * ``walltime_sec`` — Slurm ``--time=HH:MM:SS``; SGE ``-l h_rt=…``;
      PBS ``-l walltime=HH:MM:SS`` (TORQUE folds it into the same ``-l``).
    * ``gpus`` — Slurm ``--gpus=N``; SGE ``-l gpu=N``; PBS Pro
      ``select=…:ngpus=N``; TORQUE ``nodes=1:…:gpus=N``.
    * ``cpus`` — Slurm ``--cpus-per-task=N``; SGE ``-pe shared N``; PBS Pro
      ``select=…:ncpus=N``; TORQUE ``nodes=1:ppn=N``.

    The PBS grammar mirrors the engine's ``resource_flags`` (the source of
    truth) — PBS has no independent per-resource flags, so cpus/mem/gpus
    are combined into the single ``select=``/``nodes=`` chunk a command-line
    ``-l`` uses to override the script directive.

    Unknown keys are silently dropped — the planner emits documented
    keys only, and a typo'd override should not crash the whole
    resubmit. Unknown scheduler raises :class:`ValueError` (the
    backend lookup would fail anyway downstream).
    """
    if not overrides:
        return []
    s = (scheduler or "").lower()
    if s not in {"slurm", "sge", "pbspro", "torque"}:
        raise errors.SpecInvalid(
            f"render_overrides_to_extra_flags: unknown scheduler {scheduler!r}; "
            "expected 'slurm', 'sge', 'pbspro' or 'torque'"
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
    elif s == "sge":
        if isinstance(mem_mb, int) and mem_mb > 0:
            out += ["-l", f"h_data={mem_mb}M"]
        if isinstance(walltime_sec, int) and walltime_sec > 0:
            out += ["-l", f"h_rt={_format_walltime(walltime_sec)}"]
        if isinstance(gpus, int) and gpus > 0:
            out += ["-l", f"gpu={gpus}"]
        if isinstance(cpus, int) and cpus > 0:
            out += ["-pe", "shared", str(cpus)]
    elif s == "pbspro":
        # cpus/mem/gpus live in one ``select=`` chunk; walltime is separate.
        chunk = ["select=1"]
        if isinstance(cpus, int) and cpus > 0:
            chunk.append(f"ncpus={cpus}")
        if isinstance(mem_mb, int) and mem_mb > 0:
            chunk.append(f"mem={mem_mb}mb")
        if isinstance(gpus, int) and gpus > 0:
            chunk.append(f"ngpus={gpus}")
        if len(chunk) > 1:  # at least one resource beyond the bare chunk count
            out += ["-l", ":".join(chunk)]
        if isinstance(walltime_sec, int) and walltime_sec > 0:
            out += ["-l", f"walltime={_format_walltime(walltime_sec)}"]
    else:  # torque — single comma-joined ``-l`` (nodes spec + mem + walltime)
        nodes = ["nodes=1"]
        if isinstance(cpus, int) and cpus > 0:
            nodes.append(f"ppn={cpus}")
        if isinstance(gpus, int) and gpus > 0:
            nodes.append(f"gpus={gpus}")
        parts: list[str] = []
        if len(nodes) > 1:
            parts.append(":".join(nodes))
        if isinstance(mem_mb, int) and mem_mb > 0:
            parts.append(f"mem={mem_mb}mb")
        if isinstance(walltime_sec, int) and walltime_sec > 0:
            parts.append(f"walltime={_format_walltime(walltime_sec)}")
        if parts:
            out += ["-l", ",".join(parts)]
    return out


def _format_walltime(walltime_sec: int) -> str:
    """Format seconds as ``HH:MM:SS`` for Slurm/SGE walltime flags.

    Thin alias over the canonical :func:`~hpc_agent.infra.resource_format.walltime_hms`
    so the resubmit path and the SGE backend share one formatter (the old
    hand-rolled copy here agreed with that one for all positive inputs,
    and every caller below is guarded by ``walltime_sec > 0``, so this is
    a pure de-duplication with no output change).
    """
    return walltime_hms(walltime_sec)


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
    from_checkpoint: bool = False,
    bypass_preempt_throttle: bool = False,
    constraints: Any = None,
    backend_factory: Any = None,
) -> ResubmitFlowResult:
    """Execute the resubmit pipeline and emit a single result.

    Errors raise the existing :class:`~hpc_agent.errors.HpcError`
    hierarchy so the CLI adapter can surface them as typed envelope
    errors uniformly with ``submit_flow``.

    *from_checkpoint* stamps ``HPC_RESUME_FROM_CHECKPOINT=1`` into the
    cluster ``job_env`` so the dispatcher resumes each retried task from
    its latest checkpoint (#294 PR3). *bypass_preempt_throttle* skips the
    "all tasks preempted → back off" guard that fires for manual
    ``category="preempted"`` resubmits; the auto-resume composite sets it
    because resuming preempted work is its entire purpose and the resume
    cap is its backstop instead (#299).

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

    # #294 PR3 / #299: ``from_checkpoint`` resumes each retried task from its
    # latest checkpoint. The signal travels to the cluster as a job_env var the
    # dispatcher reads (``HPC_RESUME_FROM_CHECKPOINT=1``) — so it only bites on
    # an actual cluster re-run (``submit_to_cluster=True``), and a task with no
    # checkpoint just starts fresh. Single-sourced here so every caller
    # (``cmd_resubmit``, the auto-resume composite) gets the identical
    # convention rather than each hand-stamping the var.
    if from_checkpoint:
        job_env = {**(job_env or {}), "HPC_RESUME_FROM_CHECKPOINT": "1"}

    sidecar = _safe_read_sidecar(experiment_dir, run_id)

    if category == "preempted" and sidecar is not None and not bypass_preempt_throttle:
        # The "all preempted → wait for pressure to abate" throttle is the
        # MANUAL/agent posture (#299): an operator resubmitting an all-preempted
        # set by hand should back off. The auto-resume composite is the opposite
        # — the deliberate, opt-in, hard-capped path whose whole job is to resume
        # preempted work — so it passes ``bypass_preempt_throttle=True`` and
        # relies on ``max_auto_resumes`` as its backstop instead.
        _raise_if_all_preempted(sidecar, failed_task_ids)

    effective_overrides = overrides

    cluster_submitted = False
    cluster_job_ids: list[str] = []
    _clear_marker_after = False
    if submit_to_cluster:
        if script is None or backend is None or job_name is None:
            raise errors.SpecInvalid(
                "submit_to_cluster=True requires script, backend, and job_name kwargs"
            )
        from hpc_agent.state.journal import load_run as _load_run

        existing = _load_run(experiment_dir, run_id)
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
        derived_rid = request_id or derive_resubmit_request_id(
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
            from hpc_agent.state.journal import update_run_status as _update_run_status

            # On resume, rebuild the batch plan from the failed_task_ids
            # / overrides recorded at the *first* attempt — not the
            # caller's current arguments. ``resubmit_plan`` is only
            # deterministic for identical inputs, so a caller that
            # changed the failed set (or passed an explicit request_id
            # with a different set) must not shift the batch indexing
            # that ``start_batch`` relies on.
            if resuming:
                plan_failed_ids = [int(t) for t in pending.get("failed_task_ids", failed_task_ids)]
                plan_overrides = pending.get("overrides", effective_overrides)
                prior_ids = list(pending.get("job_ids", []))
            else:
                plan_failed_ids = list(failed_task_ids)
                plan_overrides = effective_overrides
                prior_ids = []
            # Batches landed so far — one job id per batch, in plan
            # order — so we resume from batch ``len(prior_ids)``.
            partial_ids: list[str] = list(prior_ids)

            def _save_marker(ids: list[str]) -> None:
                # Also write the top-level job_ids so a monitor session
                # polling the run record between a partial failure and a
                # resume sees the resubmit array jobs that already
                # landed — they would otherwise live only inside
                # pending_resubmit, which monitor does not read. EXTEND
                # rather than REPLACE for the same reason as
                # ``resubmit_failed``: keep the original array's job_ids
                # visible so monitor still sees its still-running /
                # already-complete tasks. ``pending_resubmit.job_ids``
                # stays scoped to *this* resubmit attempt (resume needs
                # to know exactly which batches landed under this rid).
                merged: dict[str, None] = dict.fromkeys(existing.job_ids or [])
                for jid in ids:
                    merged[str(jid)] = None
                _update_run_status(
                    experiment_dir,
                    run_id,
                    job_ids=list(merged),
                    pending_resubmit={
                        "request_id": derived_rid,
                        "failed_task_ids": list(plan_failed_ids),
                        "overrides": plan_overrides,
                        "job_ids": list(ids),
                    },
                )

            try:
                cluster_job_ids = _submit_resubmit_batches(
                    experiment_dir=experiment_dir,
                    run_id=run_id,
                    failed_task_ids=plan_failed_ids,
                    effective_overrides=plan_overrides,
                    # Prefer the sidecar's values (they carry any v2
                    # config that supersedes the journal), but fall back
                    # to the journal record so a missing/empty sidecar
                    # doesn't blank these and trip downstream validation.
                    ssh_target=(sidecar or {}).get("ssh_target") or existing.ssh_target,
                    remote_path=(sidecar or {}).get("remote_path") or existing.remote_path,
                    slurm_account=(sidecar or {}).get("slurm_account"),
                    slurm_cluster=(sidecar or {}).get("slurm_cluster"),
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
                _save_marker(partial_ids)
                raise
            cluster_submitted = True
            # Whole plan landed. Record the marker as *complete* (job_ids
            # == every batch) BEFORE the resubmit_failed journal stamp
            # below: if anything between here and that stamp fails, a
            # retry resumes with start_batch == len(plan.batches) — a
            # no-op — instead of re-submitting the whole plan.
            _save_marker(cluster_job_ids)
            _clear_marker_after = True

    from hpc_agent._wire.actions.resubmit import ResubmitSpec

    record, deduped, rid = resubmit_failed(
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

    if _clear_marker_after:
        # Resubmit completed and resubmit_failed stamped the request_id
        # — drop the resume marker. Best-effort: if this write fails the
        # stale marker only makes a later replay resume to a no-op.
        from hpc_agent.state.journal import update_run_status as _update_run_status

        # Best-effort marker clear (a failed write only makes a later replay
        # resume to a no-op), but narrow the catch so a programming error
        # isn't masked along with the expected journal-write failures (#165).
        with contextlib.suppress(OSError, errors.JournalCorrupt):
            _update_run_status(experiment_dir, run_id, pending_resubmit={})

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
    slurm_account: str | None = None,
    slurm_cluster: str | None = None,
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
    the production :func:`~hpc_agent.infra.backends.remote_factory.build_remote_backend`
    constructs a real SSH-backed scheduler client. Tests pass a stub
    that records calls without touching a network.
    """
    from hpc_agent.infra.constraints import ClusterConstraints

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
    # A custom-scheduler cluster may pin a SchedulerProfile in clusters.yaml;
    # honor it on recovery too (symmetric with submit_flow, which threads
    # spec.scheduler_profile). Sourced from the same cluster_cfg we read for
    # constraints. Best-effort: any failure falls back to the golden backend.
    scheduler_profile_pin: dict | None = None
    try:
        from hpc_agent.infra.clusters import load_clusters_config, load_constraints
        from hpc_agent.state.runs import read_run_sidecar

        sidecar = read_run_sidecar(experiment_dir, run_id)
        cluster_name = sidecar.get("cluster") if isinstance(sidecar, dict) else None
        if cluster_name:
            clusters = load_clusters_config()
            cluster_cfg = clusters.get(cluster_name) if isinstance(clusters, dict) else None
            if isinstance(cluster_cfg, dict):
                if effective_constraints is None:
                    effective_constraints = load_constraints(cluster_cfg)
                pin = cluster_cfg.get("scheduler_profile")
                if isinstance(pin, dict):
                    scheduler_profile_pin = pin
    except Exception:  # noqa: BLE001 — fall back to defaults on any failure
        pass

    # Fall back to the per-run experiment_meta.json pin (the unified rule's
    # source of truth) when clusters.yaml carried none — this is how an
    # ad-hoc cluster, absent from clusters.yaml, still recovers with its
    # resolved backend.
    if scheduler_profile_pin is None:
        try:
            import json as _json

            meta_p = Path(experiment_dir) / "experiment_meta.json"
            if meta_p.is_file():
                meta = _json.loads(meta_p.read_text(encoding="utf-8"))
                mp = meta.get("scheduler_profile") if isinstance(meta, dict) else None
                if isinstance(mp, dict):
                    scheduler_profile_pin = mp
        except Exception:  # noqa: BLE001 — best-effort
            pass

    plan = resubmit_plan(
        task_count=total_tasks,
        failed_task_ids=failed_task_ids,
        overrides=effective_overrides,
        constraints=effective_constraints,
    )

    extra_flags = render_overrides_to_extra_flags(scheduler, effective_overrides)

    if backend_factory is None:
        from hpc_agent.infra.backends.remote_factory import build_remote_backend
        from hpc_agent.infra.ssh_validation import validate_ssh_target

        # Validate ssh_target up front — build_remote_backend doesn't
        # double-validate (see BUG-4-10), so callers own the check. We
        # surface SpecInvalid (the same typed envelope the submit path
        # uses); inlined rather than imported from submit, since the
        # subject-imports lint forbids ops/recover → ops/submit reaches.
        try:
            validate_ssh_target(ssh_target)
        except ValueError as exc:
            raise errors.SpecInvalid(str(exc)) from exc

        backend_obj = build_remote_backend(
            backend_name=scheduler,
            script=script,
            ssh_target=ssh_target,
            remote_path=remote_path,
            pass_env_keys=tuple(job_env.keys()),
            job_env_keys=tuple(job_env.keys()),
            slurm_account=slurm_account,
            slurm_cluster=slurm_cluster,
            scheduler_profile=scheduler_profile_pin,
        )
    else:
        backend_obj = backend_factory(
            scheduler=scheduler,
            script=script,
            ssh_target=ssh_target,
            remote_path=remote_path,
            job_env_keys=tuple(job_env.keys()),
        )

    # Out-of-range guard for multi-wave resubmits (#339). On an index-bounded
    # backend (``uses_global_array_index`` False — the SSH families) a valid
    # scheduler array index is 1..max_array_size. The initial submit works around
    # the cap by submitting LOCAL ranges + a per-batch TASK_OFFSET, but a
    # resubmit replays the ACTUAL failed ids as a possibly NON-contiguous global
    # array expression (e.g. ``"1500,1700"``), which a single offset cannot
    # encode. If any failed id maps to an array index above the cap, refuse
    # rather than silently emit an out-of-range array the scheduler rejects. The
    # common case (every failed id < cap) is unaffected. Resubmit of an
    # out-of-range multi-wave id set is a documented follow-up.
    if not getattr(backend_obj, "uses_global_array_index", False):
        from hpc_agent._kernel.contract.task_id import HpcTaskId, to_array_index
        from hpc_agent.infra.constraints import ClusterConstraints

        cap = (
            effective_constraints.max_array_size
            if effective_constraints is not None
            else ClusterConstraints().max_array_size
        )
        over = sorted(
            tid for tid in failed_task_ids if int(to_array_index(HpcTaskId(int(tid)))) > cap
        )
        if over:
            raise errors.SpecInvalid(
                "resubmit of an out-of-range multi-wave task id set is not "
                f"supported yet on this backend: failed task id(s) {over} exceed "
                f"the array index cap (max_array_size={cap}). These ids only exist "
                "because the original submission waved past the cap; resubmitting "
                "them would need a non-contiguous global array expression the "
                "local-index + offset scheme cannot encode. Re-run the affected "
                "wave instead (documented follow-up)."
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

    #339 increment 3: converges onto the SHARED per-batch primitive
    :meth:`HPCBackend.submit_one` — the same ``setup_log_dir + _build_command +
    _execute_command + returncode-check + JOB_ID_REGEX`` sequence the initial
    submit (``_make_single_array_submission``) and the wave submitter
    (``submit_plan``) now use, so the duplicated qsub edge lives in one place.
    Accepts an arbitrary ``task_range`` (e.g. ``"3,7,12-14"`` — resubmit's
    non-contiguous failed ids) and threads ``extra_flags`` so the
    planner-adjusted overrides land on the qsub command line. The resubmit loop
    keeps driving this per-batch (preserving ``start_batch`` partial-resume and
    the shared ``submitted_ids`` crash-safety list) rather than handing the
    whole plan to ``submit_plan``, which cannot express partial-resume.

    ``submit_one`` raises a ``RuntimeError`` on a non-zero exit / unparseable
    id; resubmit re-wraps it as the typed :class:`RemoteCommandFailed` its
    callers expect.
    """
    try:
        return backend.submit_one(task_range, job_name, job_env, extra_flags=extra_flags, cwd=cwd)
    except RuntimeError as exc:
        raise errors.RemoteCommandFailed(f"resubmit failed for array {task_range}: {exc}") from exc


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
    except (OSError, UnicodeDecodeError, errors.HpcError):
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
