"""``submit-flow``: workflow atom that does pre-flight + rsync + deploy + qsub + record.

A workflow atom (vs a primitive atom) chains multiple SSH/scheduler/disk
operations into one composable unit with a single envelope output. Where
:func:`slash_commands.runner.submit_and_record` is the bookkeeping
primitive (writes a sidecar; never touches the cluster), ``submit_flow``
is the end-to-end pipeline: it actually rsyncs, deploys framework files,
optionally fires a 1-task canary, qsubs the array, and records to the
journal — emitting one JSON envelope at the end.

Why this exists: ``/campaign-hpc`` and other higher-level workflows
need to invoke the submit pipeline as a single CLI/Python call. The
slash-command surface (``/submit-hpc``) bundles interactive prompts
around this pipeline; the agent or another workflow can bypass the
prompts entirely by going straight to ``submit_flow``.

Idempotency
-----------
Idempotent on ``run_id`` — a replay returns ``deduped=True`` and
performs no SSH or scheduler side effects. The dedup check delegates
to :func:`runner.submit_and_record`, which has been the canonical
journal arbiter since the framework began.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from claude_hpc._internal._primitive import SideEffect, primitive
from claude_hpc.agent_cli import cmd_plan_submit
from claude_hpc.infra.backends.sge_remote import RemoteSGEBackend
from claude_hpc.infra.backends.slurm_remote import RemoteSlurmBackend
from claude_hpc.infra.remote import deploy_runtime, rsync_push, split_ssh_target, ssh_run
from claude_hpc.orchestrator.discover import discover_executors
from slash_commands import errors, runner, session
from slash_commands.runner import submit_and_record

if TYPE_CHECKING:
    from pathlib import Path

    from claude_hpc.infra.backends import HPCBackend

__all__ = ["submit_flow", "SubmitFlowResult"]


@dataclass(frozen=True)
class SubmitFlowResult:
    """Return shape of :func:`submit_flow`."""

    run_id: str
    job_ids: list[str]
    total_tasks: int
    deduped: bool
    canary_done: bool
    canary_run_id: str | None = None
    canary_job_ids: list[str] | None = None

    def to_envelope_data(self) -> dict[str, Any]:
        """Render to the shape pinned by ``schemas/submit_flow.output.json``."""
        return {
            "run_id": self.run_id,
            "job_ids": list(self.job_ids),
            "total_tasks": self.total_tasks,
            "deduped": self.deduped,
            "canary_done": self.canary_done,
            "canary_run_id": self.canary_run_id,
            "canary_job_ids": list(self.canary_job_ids) if self.canary_job_ids else None,
        }


def _split_ssh_target(ssh_target: str) -> tuple[str, str]:
    """Wrap :func:`split_ssh_target` to raise the surface-appropriate
    error type. The shared helper raises ``ValueError``; this flow
    surfaces ``SpecInvalid`` so callers see a typed envelope error.
    """
    try:
        return split_ssh_target(ssh_target)
    except ValueError as exc:
        raise errors.SpecInvalid(str(exc)) from exc


def _build_backend(
    *,
    backend_name: str,
    script: str,
    ssh_target: str,
    remote_path: str,
    pass_env_keys: tuple[str, ...] | None,
    job_env_keys: tuple[str, ...],
    slurm_account: str | None = None,
    slurm_cluster: str | None = None,
) -> HPCBackend:
    """Construct the right HPCBackend for the requested scheduler.

    Both SGE and SLURM go through the cluster's login node via SSH —
    the local backends (which assume a local ``qsub``/``sbatch`` binary)
    are never used here. submit-flow is for laptop-driven submissions
    only.
    """
    user, host = _split_ssh_target(ssh_target)

    def ssh(cmd: str):
        return ssh_run(cmd, host=host, user=user)

    if backend_name == "sge_remote":
        keys = pass_env_keys if pass_env_keys is not None else job_env_keys
        return RemoteSGEBackend(
            script=script,
            ssh_run=ssh,
            remote_repo=remote_path,
            pass_env_keys=tuple(keys),
        )
    if backend_name == "slurm":
        return RemoteSlurmBackend(
            script=script,
            ssh_run=ssh,
            remote_repo=remote_path,
            account=slurm_account,
            cluster=slurm_cluster,
        )
    raise errors.SpecInvalid(f"unknown backend: {backend_name!r}")


def _make_single_array_submission(
    backend: HPCBackend,
    *,
    job_name: str,
    total_tasks: int,
    job_env: dict[str, str],
    cwd: Path,
) -> list[str]:
    """Submit one array of size ``total_tasks`` and return the job IDs.

    Bypasses :class:`SubmissionPlan` for the simple case (no waves,
    no batching). Wave-based submissions are out of scope for v1 of
    submit-flow; callers needing them should use the legacy interactive
    ``/submit-hpc`` path or extend this function with a ``plan`` input.
    """
    backend._setup_log_dir()  # type: ignore[attr-defined]
    cmd = backend._build_command(  # type: ignore[attr-defined]
        f"1-{total_tasks}", job_name, job_env
    )
    result = backend._execute_command(cmd, job_env, cwd)  # type: ignore[attr-defined]
    if result.returncode != 0:
        stderr_msg = result.stderr.strip() if result.stderr else "(no stderr)"
        raise errors.RemoteCommandFailed(f"submit failed (exit {result.returncode}): {stderr_msg}")
    match = backend.JOB_ID_REGEX.search(result.stdout)
    if not match:
        raise errors.RemoteCommandFailed(
            f"could not parse job id from scheduler output: {result.stdout!r}"
        )
    return [match.group(1)]


@primitive(
    name="submit-flow",
    verb="workflow",
    composes=[submit_and_record, discover_executors, cmd_plan_submit],
    side_effects=[
        SideEffect("rsync", "<ssh_target>:<remote_path>"),
        SideEffect("scheduler-submit", "<cluster>"),
        SideEffect("writes-journal", "~/.claude/hpc/<repo_hash>/runs/<run_id>.json"),
    ],
    error_codes=[
        errors.SpecInvalid,
        errors.SshUnreachable,
        errors.SchedulerThrottled,
        errors.ClusterUnknown,
    ],
    idempotent=True,
    idempotency_key="run_id",
    exit_codes=[(0, "ok"), (1, "user-error"), (2, "cluster"), (3, "internal")],
)
def submit_flow(
    *,
    experiment_dir: Path,
    profile: str,
    cluster: str,
    ssh_target: str,
    remote_path: str,
    job_name: str,
    run_id: str,
    total_tasks: int,
    backend: str,
    script: str,
    job_env: dict[str, str],
    pass_env_keys: list[str] | None = None,
    canary: bool = True,
    campaign_id: str = "",
    runtime: str | None = None,
    rsync_excludes: list[str] | None = None,
    skip_preflight: bool = False,
    slurm_account: str | None = None,
    slurm_cluster: str | None = None,
    partial_ok: bool = False,
) -> SubmitFlowResult:
    """Execute the full submit pipeline and emit a single result.

    Pipeline:

    1. **Idempotency check** — if a journal record for ``run_id`` exists,
       return ``deduped=True`` immediately. No SSH, no scheduler calls.
    2. **Pre-flight gate** (skippable via ``skip_preflight``) — verifies
       SSH agent forwarding + cluster reachability. Aborts on failure.
    3. **rsync_push** — sync ``experiment_dir`` to ``remote_path``.
    4. **deploy_runtime** — scp framework files into ``remote_path/.hpc/``.
    5. **Optional canary** — submit a 1-task array (``job_name + "_canary"``,
       ``total_tasks=1``) and record it as a separate sidecar tagged with
       the same campaign. Caller is responsible for waiting and verifying
       — this atom only checks that qsub accepted the submission. Set
       ``canary=False`` to skip when the caller has just smoke-tested.
    6. **Main submit** — qsub/sbatch the full ``1-total_tasks`` array.
    7. **Record** — :func:`runner.submit_and_record` writes the per-run
       sidecar + journal entry tagged with ``campaign_id``.

    Errors raise the existing :class:`errors.HpcError` hierarchy so the
    CLI subcommand layer can convert them to error envelopes uniformly.

    *partial_ok* (default False) records ``extra.partial_ok=True`` on the
    sidecar so a downstream monitor-flow wave with at least one success
    is classified ``complete`` (not ``failed``); aggregate-flow then
    skips the failed task IDs listed under ``<run_id>.failed.json``. The
    flag mirrors the grid-sweep ``--partial-ok`` usage where one OOMing
    config shouldn't abort an N-config sweep.
    """
    # Idempotency: short-circuit before touching the cluster.
    existing = session.load_run(experiment_dir, run_id)
    if existing is not None:
        return SubmitFlowResult(
            run_id=existing.run_id,
            job_ids=list(existing.job_ids),
            total_tasks=int(existing.total_tasks),
            deduped=True,
            canary_done=False,
        )

    user, host = _split_ssh_target(ssh_target)

    # Pre-flight: a single SSH probe is the cheapest "is the cluster
    # reachable" signal. Caller can skip if they just ran `check-preflight`.
    if not skip_preflight:
        probe = ssh_run("true", host=host, user=user)
        if probe.returncode != 0:
            raise errors.SshUnreachable(
                f"pre-flight ssh probe to {ssh_target} failed (exit {probe.returncode}): "
                f"{(probe.stderr or '').strip()[:200]}"
            )

    # Push code.
    push_result = rsync_push(
        host=host,
        user=user,
        remote_path=remote_path,
        local_path=experiment_dir,
        exclude=rsync_excludes,
    )
    if push_result.returncode != 0:
        raise errors.RemoteCommandFailed(
            f"rsync push failed (exit {push_result.returncode}): "
            f"{(push_result.stderr or '').strip()[:300]}"
        )

    # Deploy framework files.
    deploy_runtime(host=host, user=user, remote_path=remote_path)

    # Honour the runtime tag — submit-spec docs say HPC_RUNTIME=uv makes
    # the cluster-side template's `uv sync` preamble fire. Caller may or
    # may not have already set this in job_env; guarantee it here when
    # the spec carries runtime="uv".
    job_env_full = dict(job_env)
    if runtime == "uv":
        job_env_full.setdefault("HPC_RUNTIME", "uv")

    # If part of a campaign, ensure HPC_CAMPAIGN_ID is forwarded to the
    # cluster — the user's tasks.py reads it at module load.
    if campaign_id:
        job_env_full.setdefault("HPC_CAMPAIGN_ID", campaign_id)

    # NFS-staging survival: if the cluster has nfs_data_dir configured
    # in clusters.yaml, thread it through as $HPC_NFS_DATA_DIR so the
    # template preamble copies the dataset into node-local SSD before
    # the executor runs. Without this, a 200-task array all open()ing
    # the same NFS files at once is the textbook way to get throttled.
    # Caller-supplied job_env wins (setdefault), so per-experiment
    # overrides still work (e.g. swapping in a different dataset).
    #
    # B-M3: scope the try/except to JUST the get_nfs_data_dir() call.
    # Previously it wrapped the whole block, so a malformed
    # ``nfs_data_dir: ""`` (which raises ValueError from the validator)
    # would silently zero out the entire cluster config — including
    # cold_start_mem_buffer, scheduler routing, and other fields the
    # campus user actually configured. Let load_clusters_config errors
    # bubble up — they were never silently survivable elsewhere — and
    # only swallow the narrow "this one optional field is malformed"
    # case, which preserves the rest of the cluster config so the run
    # survives the misconfig.
    from claude_hpc.infra.clusters import get_nfs_data_dir, load_clusters_config

    cluster_cfg = load_clusters_config().get(cluster, {})
    try:
        nfs_dir = get_nfs_data_dir(cluster_cfg) if cluster_cfg else None
    except (ValueError, TypeError):
        # nfs_data_dir is opt-in survival; a malformed value should not
        # prevent submission. The rest of cluster_cfg is still available
        # to the planner/backfill helpers.
        nfs_dir = None
    if nfs_dir:
        job_env_full.setdefault("HPC_NFS_DATA_DIR", nfs_dir)

    backend_obj = _build_backend(
        backend_name=backend,
        script=script,
        ssh_target=ssh_target,
        remote_path=remote_path,
        pass_env_keys=tuple(pass_env_keys) if pass_env_keys is not None else None,
        job_env_keys=tuple(job_env_full.keys()),
        slurm_account=slurm_account,
        slurm_cluster=slurm_cluster,
    )

    # Optional canary. Submits but does NOT wait — the slash-command
    # surface owns the elaborate "wait for terminal + verify outputs"
    # protocol. submit-flow's canary is a smoke test of the submission
    # machinery itself (qsub accepts the spec, scheduler returns a job
    # id). Higher-level callers needing full canary verification should
    # invoke /submit-hpc the slash command.
    canary_run_id: str | None = None
    canary_job_ids: list[str] | None = None
    canary_done = False
    if canary:
        canary_run_id = f"{run_id}-canary"
        canary_env = dict(job_env_full)
        canary_env["HPC_RUN_ID"] = canary_run_id
        canary_env["HPC_TASK_COUNT"] = "1"
        canary_job_ids = _make_single_array_submission(
            backend_obj,
            job_name=f"{job_name}_canary",
            total_tasks=1,
            job_env=canary_env,
            cwd=experiment_dir,
        )
        runner.submit_and_record(
            experiment_dir,
            profile=profile,
            cluster=cluster,
            ssh_target=ssh_target,
            remote_path=remote_path,
            job_name=f"{job_name}_canary",
            run_id=canary_run_id,
            job_ids=canary_job_ids,
            total_tasks=1,
            campaign_id=campaign_id,
        )
        canary_done = True

    # Main submission.
    job_ids = _make_single_array_submission(
        backend_obj,
        job_name=job_name,
        total_tasks=total_tasks,
        job_env=job_env_full,
        cwd=experiment_dir,
    )
    runner.submit_and_record(
        experiment_dir,
        profile=profile,
        cluster=cluster,
        ssh_target=ssh_target,
        remote_path=remote_path,
        job_name=job_name,
        run_id=run_id,
        job_ids=job_ids,
        total_tasks=total_tasks,
        campaign_id=campaign_id,
    )

    # Partial-ok marker: a sibling file that monitor-flow + aggregate-flow
    # consult to relax their failure semantics. Kept as a sibling of the
    # run sidecar so the two are reconcilable but the sidecar's frozen
    # schema doesn't need a bump for this opt-in flag.
    if partial_ok:
        from claude_hpc.orchestrator.runs import run_sidecar_path

        marker = run_sidecar_path(experiment_dir, run_id).with_suffix(".partial_ok")
        with contextlib.suppress(OSError):
            marker.write_text("1")

    return SubmitFlowResult(
        run_id=run_id,
        job_ids=job_ids,
        total_tasks=total_tasks,
        deduped=False,
        canary_done=canary_done,
        canary_run_id=canary_run_id,
        canary_job_ids=canary_job_ids,
    )
