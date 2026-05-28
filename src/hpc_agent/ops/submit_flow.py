"""``submit-flow``: workflow atom that does pre-flight + rsync + deploy + qsub + record.

A workflow atom (vs a primitive atom) chains multiple SSH/scheduler/disk
operations into one composable unit with a single envelope output. Where
:func:`hpc_agent.ops.submit.runner.submit_and_record` is the bookkeeping
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

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec
from hpc_agent.cli._dispatch import CliArg, CliShape, SchemaRef
from hpc_agent.infra.backends.remote_factory import build_remote_backend
from hpc_agent.infra.remote import ssh_run
from hpc_agent.infra.ssh_validation import validate_ssh_target
from hpc_agent.infra.transport import deploy_runtime, rsync_push
from hpc_agent.ops.submit.runner import submit_and_record
from hpc_agent.state.journal import load_run


def _submit_flow_handler(ns):  # type: ignore[no-untyped-def]
    """Tier 2 handler — delegates to the hand-written cmd_submit_flow shim.

    submit-flow's CLI adapter auto-routes to ``submit-flow-batch`` when
    the spec carries a ``specs`` list, injects ``--partial-ok`` into the
    spec, and emits a dry-run envelope whose shape diverges from the
    success path. None of that fits the auto-dispatcher's hook surface.
    """
    from hpc_agent.cli.submit import cmd_submit_flow

    return cmd_submit_flow(ns)


def _submit_flow_batch_handler(ns):  # type: ignore[no-untyped-def]
    """Tier 2 handler — delegates to the hand-written cmd_submit_flow_batch shim.

    submit-flow-batch runs TWO schema passes (the outer wrapper against
    ``submit_flow_batch.input.json`` + a per-entry pass against
    ``submit_flow.input.json``) and the dry-run envelope diverges from
    the success path.
    """
    from hpc_agent.cli.submit import cmd_submit_flow_batch

    return cmd_submit_flow_batch(ns)


if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from hpc_agent._wire.workflows.submit_flow_batch import SubmitFlowBatchSpec
    from hpc_agent.infra.backends import HPCBackend

__all__ = ["SubmitFlowResult", "submit_flow", "submit_flow_batch"]


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
    main_launched: bool = True

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
            "main_launched": self.main_launched,
        }


def _validate_ssh_target(ssh_target: str) -> str:
    """Adapt :func:`validate_ssh_target` to ``SpecInvalid`` for this
    flow's wire surface. The shared helper raises ``ValueError``; the
    submit flow surfaces ``SpecInvalid`` so the caller sees a typed
    envelope error. Workflow-private — ``ops/recover_flow.py`` does the
    same inline at its single call site rather than reaching into
    submit's source tree.
    """
    try:
        return validate_ssh_target(ssh_target)
    except ValueError as exc:
        raise errors.SpecInvalid(str(exc)) from exc


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


# Paths a scaffolded ``.gitignore`` marks as generated but the cluster
# node *needs*: the executor package built at Step 0 (``src/``) and the
# dispatch contract (``.hpc/tasks.py`` / ``.hpc/cli.py``). A caller derives
# rsync excludes from ``.gitignore``, so these would otherwise be stripped
# from the deploy bundle. The carve-out lives here — not in caller prose —
# so every submit path ships them. ``.hpc/.build-cache.json`` is NOT listed:
# it stays excluded (a local-build artifact the node never reads).
_GENERATED_SHIPPABLE: frozenset[str] = frozenset({"src", ".hpc/tasks.py", ".hpc/cli.py"})


def _keep_generated_shippable(excludes: list[str] | None) -> list[str] | None:
    """Drop excludes that would block shipping generated-but-needed files.

    Normalises each pattern (strips surrounding ``/``) and removes any that
    match a :data:`_GENERATED_SHIPPABLE` path, so a ``.gitignore``-derived
    exclude list still deploys ``src/`` and the ``.hpc/`` dispatch files.
    """
    if not excludes:
        return excludes
    return [e for e in excludes if e.strip().strip("/") not in _GENERATED_SHIPPABLE]


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
        exclude=_keep_generated_shippable(rsync_excludes),
    )
    if push_result.returncode != 0:
        raise errors.RemoteCommandFailed(
            f"rsync push failed (exit {push_result.returncode}): "
            f"{(push_result.stderr or '').strip()[:300]}"
        )
    deploy_runtime(ssh_target=ssh_target, remote_path=remote_path)


def _ensure_run_sidecar(experiment_dir: Path, spec: SubmitFlowSpec) -> None:
    """Guarantee the cluster-required per-run sidecar exists before rsync.

    The cluster dispatcher hard-requires ``.hpc/runs/<run_id>.json`` (it
    reads ``executor`` + ``result_dir_template`` from it) — if it is
    missing at rsync time, ``.hpc/runs/`` ships empty and every task fails
    with ``run sidecar not found``. submit-flow therefore OWNS this
    artifact instead of trusting a prior step (Step 6d / write_run_sidecar)
    to have written it (#148 / #150).

    Behaviour:

    * Sidecar already present (the normal flow — Step 6d wrote it with the
      full wave_map / config snapshot): no-op, we never overwrite it.
    * Sidecar missing + ``result_dir_template`` AND a real per-task executor
      available: synthesize a minimal-but-valid sidecar from the spec.
    * Sidecar missing + no ``result_dir_template`` **or** no real per-task
      executor (only the job script's dispatcher command is available): raise
      ``SpecInvalid`` — fail fast locally rather than ship an empty ``runs/``
      OR a self-recursive sidecar that dooms the whole array (#148 / #162).
      The caller must run write_run_sidecar first (Step 6d / wrap-entry-point)
      with the real per-task command.
    """
    from hpc_agent.state.runs import run_sidecar_path, write_run_sidecar

    target = run_sidecar_path(experiment_dir, spec.run_id)
    if target.is_file() and target.stat().st_size > 0:
        return

    if not spec.result_dir_template:
        raise errors.SpecInvalid(
            f"per-run sidecar for run_id {spec.run_id!r} is missing and the "
            "spec carries no result_dir_template, so submit-flow cannot "
            "synthesize the artifact the cluster dispatcher requires. Either "
            "run write_run_sidecar first (Step 6d / wrap-entry-point) or pass "
            "result_dir_template in the spec."
        )

    from hpc_agent import __version__ as _pkg_version
    from hpc_agent.infra.time import utcnow_iso

    job_env = spec.job_env or {}
    # job_env["EXECUTOR"] is the *job-script* command — it runs the dispatcher
    # (`python3 .hpc/_hpc_dispatch.py`), NOT a per-task command. Writing it into
    # the sidecar's `executor` makes the dispatcher run itself: the #162 live
    # incident (~8,647 retries, 8 nodes burned). There is no real per-task
    # command to synthesize from here, so fail loud — the same posture as a
    # missing result_dir_template above — rather than ship a structurally broken
    # sidecar that the old `or "...dispatch.py"` default silently produced.
    executor = job_env.get("EXECUTOR") or ""
    if (not executor) or ("_hpc_dispatch.py" in executor) or ("dispatch.py" in executor):
        raise errors.SpecInvalid(
            f"per-run sidecar for run_id {spec.run_id!r} is missing and submit-flow "
            f"cannot synthesize a valid one: the only available executor ({executor!r}) "
            "is the job-script command (it runs the dispatcher), not a per-task command, "
            "so synthesizing it would make the dispatcher run itself (#162). Run "
            "write_run_sidecar first (Step 6d / wrap-entry-point) with the real per-task "
            "command (e.g. `python train.py --seed $SEED`)."
        )
    cmd_sha = job_env.get("HPC_CMD_SHA", "")

    # tasks_py_sha is provenance only (drift detection); compute it from the
    # local tasks.py when present, else leave empty — the dispatcher does
    # not require it.
    tasks_py_sha = ""
    tasks_py = experiment_dir / ".hpc" / "tasks.py"
    if tasks_py.is_file():
        from hpc_agent.state.run_sha import compute_tasks_py_sha

        with contextlib.suppress(Exception):
            tasks_py_sha = compute_tasks_py_sha(tasks_py)

    resources = spec.resources.model_dump(exclude_none=True) if spec.resources else None

    write_run_sidecar(
        experiment_dir,
        run_id=spec.run_id,
        cmd_sha=cmd_sha,
        hpc_agent_version=_pkg_version or "",
        submitted_at=utcnow_iso(),
        executor=executor,
        result_dir_template=spec.result_dir_template,
        task_count=int(spec.total_tasks),
        tasks_py_sha=tasks_py_sha,
        cluster=spec.cluster,
        profile=spec.profile,
        remote_path=spec.remote_path,
        campaign_id=spec.campaign_id or None,
        runtime=spec.runtime,
        resources=resources or None,
    )


def _mirror_canary_sidecar(experiment_dir: Path, main_run_id: str, canary_run_id: str) -> None:
    """Ensure the canary's per-run sidecar exists by mirroring the main run's.

    The dispatcher hard-requires ``.hpc/runs/<run_id>.json``; the canary uses
    run_id ``<main>-canary``, which Step 6d never writes and
    :func:`_ensure_run_sidecar` only covers for the main spec — so the canary
    errored ``sidecar not found`` and the gate was a no-op (#160 / #162). Copy
    the main sidecar's per-task executor + result_dir_template to the canary
    path (``task_count=1``) so the canary dispatches the SAME command. No-op
    when the canary sidecar already exists or the main one is unreadable.
    """
    from hpc_agent.infra.time import utcnow_iso
    from hpc_agent.state.runs import read_run_sidecar, run_sidecar_path, write_run_sidecar

    target = run_sidecar_path(experiment_dir, canary_run_id)
    if target.is_file() and target.stat().st_size > 0:
        return
    try:
        main = read_run_sidecar(experiment_dir, main_run_id)
    except Exception:  # noqa: BLE001 — best-effort mirror; a missing main is handled below
        return
    executor = main.get("executor")
    result_dir_template = main.get("result_dir_template")
    if not executor or not result_dir_template:
        return  # main sidecar lacks the dispatch essentials; nothing to mirror
    write_run_sidecar(
        experiment_dir,
        run_id=canary_run_id,
        cmd_sha=str(main.get("cmd_sha", "")),
        hpc_agent_version=str(main.get("hpc_agent_version", "")),
        submitted_at=utcnow_iso(),
        executor=str(executor),
        result_dir_template=str(result_dir_template),
        task_count=1,
        tasks_py_sha=str(main.get("tasks_py_sha", "")),
        wave_map={"0": [0]},
        cluster=main.get("cluster"),
        profile=main.get("profile"),
        remote_path=main.get("remote_path"),
        campaign_id=main.get("campaign_id") or None,
        runtime=main.get("runtime"),
        resources=main.get("resources") or None,
    )


def _augment_job_env(
    *,
    job_env: dict[str, str],
    runtime: str | None,
    campaign_id: str | None,
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
    from hpc_agent.infra.clusters import get_nfs_data_dir, load_clusters_config

    cluster_cfg = load_clusters_config().get(cluster, {})
    try:
        nfs_dir = get_nfs_data_dir(cluster_cfg) if cluster_cfg else None
    except (errors.SpecInvalid, TypeError):
        # Treat a malformed nfs_data_dir as "no NFS staging" rather
        # than failing the whole submission — the rest of the cluster
        # config (scheduler, cold_start_mem_buffer, ...) is still
        # usable. Pre-migration this caught the underlying
        # ``ValueError``; the typed migration replaced it with
        # ``SpecInvalid``.
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
    resources: object = None,
) -> list[str]:
    """Submit one array of size ``total_tasks`` and return the job IDs.

    Bypasses :class:`SubmissionPlan` for the simple case (no waves,
    no batching). Wave-based submissions are out of scope for v1 of
    submit-flow; callers needing them should use the legacy interactive
    ``/submit-hpc`` path or extend this function with a ``plan`` input.

    *resources* (a ``SubmitResources`` or ``None``) is translated by the
    backend into scheduler resource flags; ``None``/empty emits none, so
    the template directives apply unchanged.
    """
    backend._setup_log_dir()  # type: ignore[attr-defined]
    cmd = backend._build_command(  # type: ignore[attr-defined]
        f"1-{total_tasks}", job_name, job_env, extra_flags=backend.resource_flags(resources)
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
    # ``submit_and_record`` is the only atom this workflow actually invokes
    # at runtime. ``discover_executors`` is imported
    # for type hints / pre-submit advisory paths but not in the composition
    # itself; advertising it here previously made operations.json over-
    # promise the workflow's dependency graph.
    composes=[submit_and_record],
    side_effects=[
        SideEffect("sync-push", "<ssh_target>:<remote_path>"),
        SideEffect("scheduler-submit", "<cluster>"),
        SideEffect("writes-journal", "~/.claude/hpc/<repo_hash>/runs/<run_id>.json"),
    ],
    # ``SchedulerThrottled`` was declared but never raised: real
    # throttling currently surfaces as ``RemoteCommandFailed``. Removed
    # to stop callers wiring retry policy against a code that never
    # fires. ``RemoteCommandFailed`` IS raised by ssh_run helpers in
    # this primitive's transitive path.
    error_codes=[
        errors.SpecInvalid,
        errors.SshUnreachable,
        errors.RemoteCommandFailed,
        errors.ClusterUnknown,
    ],
    idempotent=True,
    idempotency_key="run_id",
    exit_codes=[(0, "ok"), (1, "user-error"), (2, "cluster"), (3, "internal")],
    cli=CliShape(
        help=(
            "Workflow atom: pre-flight + rsync + deploy + qsub + record in "
            "one shot. Auto-dispatches to submit-flow-batch when the spec "
            "is a {specs: [...]} object — callers always invoke this one "
            "subcommand whether the iteration emits 1 spec or N. Idempotent "
            "on run_id (or per-spec run_id when batched)."
        ),
        requires_ssh=True,
        spec_arg=True,
        spec_required=True,
        schema_ref=SchemaRef(input="submit_flow"),
        experiment_dir_arg=True,
        args=(
            CliArg(
                "--dry-run",
                action="store_true",
                help=("Validate the spec and report what would be launched; no SSH/rsync/qsub."),
            ),
            CliArg(
                "--partial-ok",
                action="store_true",
                help=(
                    "Tolerate per-task failures: when the wave finishes, classify "
                    "as `complete` if at least one task succeeded; record failed "
                    "task IDs in <run_id>.failed.json so aggregate-flow can skip "
                    "them. Without this flag (the default), any failure aborts "
                    "the wave with lifecycle_state=failed."
                ),
            ),
        ),
        handler=_submit_flow_handler,
    ),
    agent_facing=True,
)
def submit_flow(
    experiment_dir: Path,
    *,
    spec: SubmitFlowSpec,
) -> SubmitFlowResult:
    """Execute the full submit pipeline and emit a single result.

    Pipeline:

    1. **Idempotency check** — if a journal record for ``spec.run_id``
       exists, return ``deduped=True`` immediately. No SSH, no scheduler
       calls.
    2. **Pre-flight gate** (skippable via ``spec.skip_preflight``) —
       verifies SSH agent forwarding + cluster reachability. Aborts on
       failure.
    3. **rsync_push** — sync ``experiment_dir`` to ``spec.remote_path``.
    4. **deploy_runtime** — scp framework files into
       ``<remote_path>/.hpc/``.
    5. **Optional canary** — submit a 1-task array (``job_name +
       "_canary"``, ``total_tasks=1``) and record it as a separate sidecar
       tagged with the same campaign. Caller waits and verifies — this
       atom only checks that qsub accepted the submission. Set
       ``spec.canary=False`` to skip when the caller has just
       smoke-tested.
    6. **Main submit** — qsub/sbatch the full ``1-total_tasks`` array.
    7. **Record** — :func:`runner.submit_and_record` writes the per-run
       sidecar + journal entry tagged with ``spec.campaign_id``.

    Errors raise the existing :class:`errors.HpcError` hierarchy so the
    CLI subcommand layer can convert them to error envelopes uniformly.

    ``spec.partial_ok`` records ``extra.partial_ok=True`` on the sidecar
    so a downstream monitor-flow wave with at least one success is
    classified ``complete`` (not ``failed``); aggregate-flow then skips
    the failed task IDs listed under ``<run_id>.failed.json``.
    """
    from hpc_agent._wire.workflows.submit_flow_batch import (
        SubmitFlowBatchSpec as _BatchSpec,
    )

    batch_spec = _BatchSpec(
        specs=[spec],
        rsync_excludes=spec.rsync_excludes,
        skip_preflight=spec.skip_preflight,
    )
    return submit_flow_batch(experiment_dir, spec=batch_spec)[0]


def _dedup_existing(experiment_dir: Path, spec: SubmitFlowSpec) -> SubmitFlowResult | None:
    """Return a deduped SubmitFlowResult if a journal record already exists."""
    existing = load_run(experiment_dir, spec.run_id)
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
    spec: SubmitFlowSpec,
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
    backend_obj = build_remote_backend(
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
        existing_canary = load_run(experiment_dir, canary_run_id)
        if existing_canary is not None:
            # Replay: a prior call landed the canary but failed before
            # recording the main run, so the main-run dedup check (keyed
            # on spec.run_id) misses it. Reuse the recorded canary
            # job_ids instead of firing a duplicate canary qsub —
            # submit_flow is documented idempotent on run_id.
            canary_job_ids = list(existing_canary.job_ids)
            canary_done = True
        else:
            # Mirror the main sidecar to <run_id>-canary.json so the canary
            # dispatches the SAME per-task executor (#162a) — otherwise it
            # errors 'sidecar not found' and the canary gate is a no-op (#160).
            _mirror_canary_sidecar(experiment_dir, spec.run_id, canary_run_id)
            canary_env = dict(job_env_full)
            canary_env["HPC_RUN_ID"] = canary_run_id
            canary_env["HPC_TASK_COUNT"] = "1"
            canary_job_ids = _make_single_array_submission(
                backend_obj,
                job_name=f"{spec.job_name}_canary",
                total_tasks=1,
                job_env=canary_env,
                cwd=experiment_dir,
                resources=spec.resources,
            )
            from hpc_agent._wire.actions.submit import SubmitSpec as _SubmitSpec

            submit_and_record(
                experiment_dir,
                spec=_SubmitSpec(
                    profile=spec.profile,
                    cluster=spec.cluster,
                    ssh_target=spec.ssh_target,
                    remote_path=spec.remote_path,
                    job_name=f"{spec.job_name}_canary",
                    run_id=canary_run_id,
                    job_ids=canary_job_ids,
                    total_tasks=1,
                    campaign_id=spec.campaign_id or None,
                ),
            )
            canary_done = True

    if spec.canary_only:
        # Two-phase canary gate (#160): the canary is submitted; do NOT launch
        # the main array. The caller verifies the canary (verify-canary) and
        # re-invokes submit-flow with canary=false to launch the main only on
        # success — so a broken dispatch can't sail past the canary.
        return SubmitFlowResult(
            run_id=spec.run_id,
            job_ids=[],
            total_tasks=spec.total_tasks,
            deduped=False,
            canary_done=canary_done,
            canary_run_id=canary_run_id,
            canary_job_ids=canary_job_ids,
            main_launched=False,
        )

    job_ids = _make_single_array_submission(
        backend_obj,
        job_name=spec.job_name,
        total_tasks=spec.total_tasks,
        job_env=job_env_full,
        cwd=experiment_dir,
        resources=spec.resources,
    )
    from hpc_agent._wire.actions.submit import SubmitSpec as _SubmitSpec

    submit_and_record(
        experiment_dir,
        spec=_SubmitSpec(
            profile=spec.profile,
            cluster=spec.cluster,
            ssh_target=spec.ssh_target,
            remote_path=spec.remote_path,
            job_name=spec.job_name,
            run_id=spec.run_id,
            job_ids=job_ids,
            total_tasks=spec.total_tasks,
            campaign_id=spec.campaign_id or None,
        ),
    )

    if spec.partial_ok:
        from hpc_agent.state.runs import run_sidecar_path

        marker = run_sidecar_path(experiment_dir, spec.run_id).with_suffix(".partial_ok")
        with contextlib.suppress(OSError):
            marker.write_text("1", encoding="utf-8")

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
    # ``submit_and_record`` is the only atom this workflow actually invokes
    # at runtime. ``discover_executors`` is imported
    # for type hints / pre-submit advisory paths but not in the composition
    # itself; advertising it here previously made operations.json over-
    # promise the workflow's dependency graph.
    composes=[submit_and_record],
    side_effects=[
        SideEffect("sync-push", "<ssh_target>:<remote_path>"),
        SideEffect("scheduler-submit", "<cluster> (one qsub per spec)"),
        SideEffect("writes-journal", "~/.claude/hpc/<repo_hash>/runs/<run_id>.json (per spec)"),
    ],
    # See submit-flow above: ``SchedulerThrottled`` removed because
    # nothing actually raises it; real throttling surfaces as
    # ``RemoteCommandFailed``.
    error_codes=[
        errors.SpecInvalid,
        errors.SshUnreachable,
        errors.RemoteCommandFailed,
        errors.ClusterUnknown,
    ],
    idempotent=True,
    idempotency_key="specs.run_id",
    exit_codes=[(0, "ok"), (1, "user-error"), (2, "cluster"), (3, "internal")],
    cli=CliShape(
        help=(
            "Workflow atom: rsync + deploy ONCE, then qsub N specs sharing "
            "the same (ssh_target, remote_path). Use whenever a campaign or "
            "sweep submits >1 specs to the same cluster — bundles 13×N ssh "
            "handshakes into ~3 (rsync + deploy + multiplexed qsubs). Spec "
            "file is a JSON list."
        ),
        requires_ssh=True,
        spec_arg=True,
        spec_required=True,
        schema_ref=SchemaRef(input="submit_flow_batch"),
        experiment_dir_arg=True,
        args=(
            CliArg(
                "--dry-run",
                action="store_true",
                help="Validate the batch + report shared targets; no SSH/rsync/qsub.",
            ),
        ),
        handler=_submit_flow_batch_handler,
    ),
    agent_facing=True,
)
def submit_flow_batch(
    experiment_dir: Path,
    *,
    spec: SubmitFlowBatchSpec,
) -> list[SubmitFlowResult]:
    """Submit N specs that share ``(ssh_target, remote_path)`` in one shot.

    The Pydantic ``SubmitFlowBatchSpec`` is the canonical wire +
    Python authoring surface; ``spec.specs`` is a list of full
    :class:`SubmitFlowSpec` models (the same type the standalone
    ``submit-flow`` atom takes). ``spec.rsync_excludes`` and
    ``spec.skip_preflight`` apply once across the bundle.

    The motivating problem: a campaign-time fan-out of N submissions
    used to do N × (rsync + deploy_runtime + qsub), which sent ~13×N
    ssh handshakes at the cluster's sshd and tripped MaxStartups
    (CARC, typically). The bundle collapses that to:

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

    ``spec.specs`` MUST share ``ssh_target`` and ``remote_path`` —
    different targets/paths can't share an rsync. Heterogeneous batches
    raise :class:`errors.SpecInvalid`; the caller (campaign driver /
    agent) is responsible for grouping specs by ``(ssh_target,
    remote_path)`` before calling.

    Order of returned results matches the order of ``spec.specs``.
    """
    rsync_excludes = list(spec.rsync_excludes) if spec.rsync_excludes is not None else None
    skip_preflight = spec.skip_preflight if spec.skip_preflight is not None else False
    inner_specs = list(spec.specs)

    # Per-repo advisory submit lock: serialize multiple `submit-flow` /
    # `submit-flow-batch` invocations against the same experiment so two
    # shells firing simultaneously don't BOTH fan out N qsubs at the
    # cluster's sshd. The lock is advisory (other code paths don't take
    # it) and per-repo (`<journal_home>/.submit_lock`); cross-cluster
    # parallelism is still allowed when each cluster has its own
    # experiment_dir. Disable via ``HPC_SUBMIT_NO_LOCK=1`` — kept
    # narrowly for (a) the test suite, which exercises submit_flow in
    # parallel with mocked subprocess so there's no real qsub to race,
    # and (b) operators who deliberately want concurrent submits and
    # have confirmed the cluster's sshd / scheduler tolerates the
    # burst. Disabling outside those two cases risks a scheduler-
    # throttling stampede; see ``docs/reference/env-vars.md``.
    import os

    from hpc_agent.infra import io
    from hpc_agent.state.run_record import journal_dir

    use_lock = os.environ.get("HPC_SUBMIT_NO_LOCK") != "1"
    lock_path = journal_dir(experiment_dir) / ".submit_lock"
    lock_ctx = io.advisory_flock(lock_path) if use_lock else _noop_lock_ctx()
    with lock_ctx:
        return _submit_flow_batch_locked(
            experiment_dir=experiment_dir,
            specs=inner_specs,
            rsync_excludes=rsync_excludes,
            skip_preflight=skip_preflight,
        )


@contextlib.contextmanager
def _noop_lock_ctx() -> Iterator[bool]:
    """Stand-in for advisory_flock when HPC_SUBMIT_NO_LOCK=1."""
    yield True


def _submit_flow_batch_locked(
    *,
    experiment_dir: Path,
    specs: list[SubmitFlowSpec],
    rsync_excludes: list[str] | None,
    skip_preflight: bool,
) -> list[SubmitFlowResult]:
    """Body of :func:`submit_flow_batch`, executed under the per-repo lock."""
    # Auto-cleanup: drop sidecars from earlier failed batches before doing
    # anything else. Without this, a half-baked sidecar from yesterday's
    # rate-limited submit would still surface to find_run_by_cmd_sha and
    # to the agent's resume-detection prompts. The prune is silent on
    # success (returns []); if it deletes anything, the cluster traffic
    # we're about to send is fresh anyway.
    #
    # ``min_age_seconds=0`` is safe here: the per-repo lock above
    # serialises submit_flow_batch invocations against the same
    # experiment, so the only sidecars present at this point are from
    # PRIOR batches (which had to complete or fail before releasing the
    # lock). The default min_age_seconds guard is for ad-hoc invocations
    # that don't hold the lock and could race a concurrent submit.
    #
    # ``exclude`` protects the run_ids in THIS batch: the slash flow
    # writes each run's sidecar jobless at Step 6d *before* calling
    # submit_flow_batch, so those sidecars are present inside the lock
    # and are indistinguishable (jobless + journal-less) from a prior
    # failed batch's orphan. Without the exclude the prune would delete
    # the very sidecars we're about to finalize post-qsub. The canary
    # sibling (``{run_id}-canary``) is written the same way.
    from hpc_agent.state.runs import prune_orphan_sidecars

    protected = {s.run_id for s in specs} | {f"{s.run_id}-canary" for s in specs}
    prune_orphan_sidecars(experiment_dir, min_age_seconds=0, exclude=protected)

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

    # Guarantee the cluster-required per-run sidecar exists for every
    # fresh spec BEFORE rsync — submit-flow owns this artifact rather than
    # trusting a prior step to have written it. Missing + synthesizable →
    # written here; missing + not synthesizable → fail fast locally
    # (see _ensure_run_sidecar). #148 / #150.
    for i in fresh_indices:
        _ensure_run_sidecar(experiment_dir, specs[i])

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
    #
    # If spec ``i`` raises mid-loop, specs ``0..i-1`` are already on the
    # cluster (qsubbed AND journal-recorded by submit_and_record); we
    # can't recall them. Attach the partial result list to the
    # exception so the caller can recover state (which run_ids landed,
    # which to retry) instead of getting a bare raise with no
    # accounting.
    for i in fresh_indices:
        try:
            results[i] = _submit_one_spec(experiment_dir=experiment_dir, spec=specs[i])
        except Exception as exc:
            # Mutate the exception to carry the partial results. The
            # caller can branch on ``hasattr(exc, "partial_submit_results")``
            # to recover the (succeeded, failed_index) split.
            partial = [r for r in results if r is not None]
            exc.partial_submit_results = partial  # type: ignore[attr-defined]
            exc.failed_spec_index = i  # type: ignore[attr-defined]
            raise
    # mypy: every slot is now non-None.
    return [r for r in results if r is not None]
