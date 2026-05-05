"""``submit-flow``: workflow atom that does pre-flight + rsync + deploy + qsub + record.

A workflow atom (vs a primitive atom) chains multiple SSH/scheduler/disk
operations into one composable unit with a single envelope output. Where
:func:`claude_hpc.runner.submit_and_record` is the bookkeeping
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

from claude_hpc import errors, runner
from claude_hpc._internal import session
from claude_hpc._internal._primitive import SideEffect, primitive
from claude_hpc.agent_cli import cmd_plan_submit
from claude_hpc.infra.backends.sge_remote import RemoteSGEBackend
from claude_hpc.infra.backends.slurm_remote import RemoteSlurmBackend
from claude_hpc.infra.remote import deploy_runtime, rsync_push, ssh_run, validate_ssh_target
from claude_hpc.runner import submit_and_record
from claude_hpc.state.discover import discover_executors

if TYPE_CHECKING:
    from pathlib import Path

    from claude_hpc.infra.backends import HPCBackend

__all__ = ["SubmitSpec", "SubmitFlowResult", "submit_flow", "submit_flow_batch"]


@dataclass(frozen=True)
class SubmitSpec:
    """One leaf submission inside a (possibly batched) submit pipeline.

    Mirrors the kwargs that :func:`submit_flow` already accepts, just
    bundled into a frozen record so :func:`submit_flow_batch` can fan
    out N submissions sharing one ``(ssh_target, remote_path)`` without
    repeating the rsync + deploy. Field-for-field equivalence with
    :func:`submit_flow`'s signature is intentional — call sites can
    construct a ``SubmitSpec`` from the same dict they pass today.
    """

    profile: str
    cluster: str
    ssh_target: str
    remote_path: str
    job_name: str
    run_id: str
    total_tasks: int
    backend: str
    script: str
    job_env: dict[str, str]
    pass_env_keys: list[str] | None = None
    canary: bool = True
    campaign_id: str = ""
    runtime: str | None = None
    slurm_account: str | None = None
    slurm_cluster: str | None = None
    partial_ok: bool = False


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


def _validate_ssh_target(ssh_target: str) -> str:
    """Wrap :func:`validate_ssh_target` to raise the surface-appropriate
    error type. The shared helper raises ``ValueError``; this flow
    surfaces ``SpecInvalid`` so callers see a typed envelope error.
    """
    try:
        return validate_ssh_target(ssh_target)
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
    _validate_ssh_target(ssh_target)

    def ssh(cmd: str):
        return ssh_run(cmd, ssh_target=ssh_target)

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


def _preflight_probe(ssh_target: str, *, skip: bool) -> None:
    """Single ssh probe to verify cluster reachability. Caller may skip."""
    if skip:
        return
    probe = ssh_run("true", ssh_target=ssh_target)
    if probe.returncode != 0:
        raise errors.SshUnreachable(
            f"pre-flight ssh probe to {ssh_target} failed (exit {probe.returncode}): "
            f"{(probe.stderr or '').strip()[:200]}"
        )


def _push_and_deploy(
    *,
    experiment_dir: Path,
    ssh_target: str,
    remote_path: str,
    rsync_excludes: list[str] | None,
) -> None:
    """rsync_push + deploy_runtime — the expensive ssh fan-out, done once.

    Extracted so :func:`submit_flow_batch` can run it once across N
    specs that share ``(ssh_target, remote_path)``. The previous
    architecture re-ran both for every spec, which is what tripped
    cluster sshd MaxStartups under campaign-time fan-out (see commit
    0c99e1f / the SSH-backoff commit).
    """
    push_result = rsync_push(
        ssh_target=ssh_target,
        remote_path=remote_path,
        local_path=experiment_dir,
        exclude=rsync_excludes,
    )
    if push_result.returncode != 0:
        raise errors.RemoteCommandFailed(
            f"rsync push failed (exit {push_result.returncode}): "
            f"{(push_result.stderr or '').strip()[:300]}"
        )
    deploy_runtime(ssh_target=ssh_target, remote_path=remote_path)


def _augment_job_env(
    *,
    job_env: dict[str, str],
    runtime: str | None,
    campaign_id: str,
    cluster: str,
) -> dict[str, str]:
    """Layer the framework-driven env vars on top of the caller's job_env.

    Three augmentations: ``HPC_RUNTIME=uv`` when the spec asks for it,
    ``HPC_CAMPAIGN_ID`` when the run is part of a closed-loop campaign,
    and ``HPC_NFS_DATA_DIR`` from the cluster's ``nfs_data_dir`` setting
    (NFS-staging survival). Caller-supplied keys win via setdefault.
    """
    out = dict(job_env)
    if runtime == "uv":
        out.setdefault("HPC_RUNTIME", "uv")
    if campaign_id:
        out.setdefault("HPC_CAMPAIGN_ID", campaign_id)
    from claude_hpc.infra.clusters import get_nfs_data_dir, load_clusters_config

    cluster_cfg = load_clusters_config().get(cluster, {})
    try:
        nfs_dir = get_nfs_data_dir(cluster_cfg) if cluster_cfg else None
    except (ValueError, TypeError):
        nfs_dir = None
    if nfs_dir:
        out.setdefault("HPC_NFS_DATA_DIR", nfs_dir)
    return out


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
    spec = SubmitSpec(
        profile=profile,
        cluster=cluster,
        ssh_target=ssh_target,
        remote_path=remote_path,
        job_name=job_name,
        run_id=run_id,
        total_tasks=total_tasks,
        backend=backend,
        script=script,
        job_env=dict(job_env),
        pass_env_keys=list(pass_env_keys) if pass_env_keys is not None else None,
        canary=canary,
        campaign_id=campaign_id,
        runtime=runtime,
        slurm_account=slurm_account,
        slurm_cluster=slurm_cluster,
        partial_ok=partial_ok,
    )
    results: list[SubmitFlowResult] = submit_flow_batch(
        experiment_dir=experiment_dir,
        specs=[spec],
        rsync_excludes=rsync_excludes,
        skip_preflight=skip_preflight,
    )
    return results[0]


def _dedup_existing(experiment_dir: Path, spec: SubmitSpec) -> SubmitFlowResult | None:
    """Return a deduped SubmitFlowResult if a journal record already exists."""
    existing = session.load_run(experiment_dir, spec.run_id)
    if existing is None:
        return None
    return SubmitFlowResult(
        run_id=existing.run_id,
        job_ids=list(existing.job_ids),
        total_tasks=int(existing.total_tasks),
        deduped=True,
        canary_done=False,
    )


def _submit_one_spec(
    *,
    experiment_dir: Path,
    spec: SubmitSpec,
) -> SubmitFlowResult:
    """Per-spec submission work — backend build + (canary?) + main qsub + record.

    The expensive shared steps (preflight + rsync + deploy) MUST already
    have run on this ``(ssh_target, remote_path)`` before reaching here;
    :func:`submit_flow_batch` is responsible for that prelude.
    """
    job_env_full = _augment_job_env(
        job_env=spec.job_env,
        runtime=spec.runtime,
        campaign_id=spec.campaign_id,
        cluster=spec.cluster,
    )
    backend_obj = _build_backend(
        backend_name=spec.backend,
        script=spec.script,
        ssh_target=spec.ssh_target,
        remote_path=spec.remote_path,
        pass_env_keys=tuple(spec.pass_env_keys) if spec.pass_env_keys is not None else None,
        job_env_keys=tuple(job_env_full.keys()),
        slurm_account=spec.slurm_account,
        slurm_cluster=spec.slurm_cluster,
    )

    canary_run_id: str | None = None
    canary_job_ids: list[str] | None = None
    canary_done = False
    if spec.canary:
        canary_run_id = f"{spec.run_id}-canary"
        canary_env = dict(job_env_full)
        canary_env["HPC_RUN_ID"] = canary_run_id
        canary_env["HPC_TASK_COUNT"] = "1"
        canary_job_ids = _make_single_array_submission(
            backend_obj,
            job_name=f"{spec.job_name}_canary",
            total_tasks=1,
            job_env=canary_env,
            cwd=experiment_dir,
        )
        runner.submit_and_record(
            experiment_dir,
            profile=spec.profile,
            cluster=spec.cluster,
            ssh_target=spec.ssh_target,
            remote_path=spec.remote_path,
            job_name=f"{spec.job_name}_canary",
            run_id=canary_run_id,
            job_ids=canary_job_ids,
            total_tasks=1,
            campaign_id=spec.campaign_id,
        )
        canary_done = True

    job_ids = _make_single_array_submission(
        backend_obj,
        job_name=spec.job_name,
        total_tasks=spec.total_tasks,
        job_env=job_env_full,
        cwd=experiment_dir,
    )
    runner.submit_and_record(
        experiment_dir,
        profile=spec.profile,
        cluster=spec.cluster,
        ssh_target=spec.ssh_target,
        remote_path=spec.remote_path,
        job_name=spec.job_name,
        run_id=spec.run_id,
        job_ids=job_ids,
        total_tasks=spec.total_tasks,
        campaign_id=spec.campaign_id,
    )

    if spec.partial_ok:
        from claude_hpc.state.runs import run_sidecar_path

        marker = run_sidecar_path(experiment_dir, spec.run_id).with_suffix(".partial_ok")
        with contextlib.suppress(OSError):
            marker.write_text("1")

    return SubmitFlowResult(
        run_id=spec.run_id,
        job_ids=job_ids,
        total_tasks=spec.total_tasks,
        deduped=False,
        canary_done=canary_done,
        canary_run_id=canary_run_id,
        canary_job_ids=canary_job_ids,
    )


@primitive(
    name="submit-flow-batch",
    verb="workflow",
    composes=[submit_and_record, discover_executors, cmd_plan_submit],
    side_effects=[
        SideEffect("rsync", "<ssh_target>:<remote_path>"),
        SideEffect("scheduler-submit", "<cluster> (one qsub per spec)"),
        SideEffect("writes-journal", "~/.claude/hpc/<repo_hash>/runs/<run_id>.json (per spec)"),
    ],
    error_codes=[
        errors.SpecInvalid,
        errors.SshUnreachable,
        errors.SchedulerThrottled,
        errors.ClusterUnknown,
    ],
    idempotent=True,
    idempotency_key="(spec.run_id for spec in specs)",
    exit_codes=[(0, "ok"), (1, "user-error"), (2, "cluster"), (3, "internal")],
)
def submit_flow_batch(
    *,
    experiment_dir: Path,
    specs: list[SubmitSpec],
    rsync_excludes: list[str] | None = None,
    skip_preflight: bool = False,
) -> list[SubmitFlowResult]:
    """Submit N specs that share ``(ssh_target, remote_path)`` in one shot.

    The motivating problem: a campaign-time fan-out of N submissions used
    to do N × (rsync + deploy_runtime + qsub), which sent ~13×N ssh
    handshakes at the cluster's sshd and tripped MaxStartups (CARC,
    typically). The bundle collapses that to:

    * 1 ssh probe (preflight)
    * 1 ``rsync_push`` (the codebase is identical across specs)
    * 1 ``deploy_runtime`` (the framework files are identical across specs)
    * N × (qsub + ``submit_and_record``) — sequential, but reusing the
      ssh ControlMaster socket established by the probe, so each
      additional qsub is ~free.

    Specs that already have a journal record are deduped up front and
    contribute a ``deduped=True`` :class:`SubmitFlowResult` without any
    cluster traffic — the same idempotency contract :func:`submit_flow`
    has always offered, applied per-spec.

    *specs* MUST share ``ssh_target`` and ``remote_path`` — different
    targets/paths can't share an rsync. Heterogeneous batches raise
    :class:`errors.SpecInvalid`; the caller (campaign driver / agent)
    is responsible for grouping specs by ``(ssh_target, remote_path)``
    before calling.

    Order of returned results matches the order of *specs*.
    """
    if not specs:
        return []

    # Single-target invariant: rsync + deploy can only target one place.
    targets = {(s.ssh_target, s.remote_path) for s in specs}
    if len(targets) > 1:
        raise errors.SpecInvalid(
            f"submit_flow_batch requires all specs to share (ssh_target, remote_path); "
            f"got {len(targets)} distinct combinations: {sorted(targets)}"
        )

    # Per-spec idempotency: dedup against the journal up front, never
    # touch the cluster for already-submitted run_ids.
    results: list[SubmitFlowResult | None] = [_dedup_existing(experiment_dir, s) for s in specs]
    fresh_indices = [i for i, r in enumerate(results) if r is None]
    if not fresh_indices:
        # Every spec was already on the journal — return the deduped
        # results without firing rsync/deploy. ``# type: ignore`` would
        # otherwise be needed because mypy can't see the None elimination.
        return [r for r in results if r is not None]

    # Shared prelude: one ssh probe, one rsync, one deploy. This is the
    # whole point of the batch — collapse N × (probe + rsync + deploy)
    # into 1 × (probe + rsync + deploy), then fire N qsubs reusing the
    # ssh ControlMaster.
    ssh_target, remote_path = next(iter(targets))
    _validate_ssh_target(ssh_target)
    _preflight_probe(ssh_target, skip=skip_preflight)
    _push_and_deploy(
        experiment_dir=experiment_dir,
        ssh_target=ssh_target,
        remote_path=remote_path,
        rsync_excludes=rsync_excludes,
    )

    # Per-spec submission work.
    for i in fresh_indices:
        results[i] = _submit_one_spec(experiment_dir=experiment_dir, spec=specs[i])
    # mypy: every slot is now non-None.
    return [r for r in results if r is not None]
