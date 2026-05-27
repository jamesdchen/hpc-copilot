"""Submit-time runner primitives."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.submit import SubmitSpec
from hpc_agent.cli._dispatch import CliArg, CliShape
from hpc_agent.infra.time import utcnow_iso
from hpc_agent.state.journal import load_run, upsert_run
from hpc_agent.state.run_record import RunRecord
from hpc_agent.state.runs import find_run_by_cmd_sha, read_run_sidecar


def _submit_spec_handler(ns):  # type: ignore[no-untyped-def]
    """Tier 2 handler — delegates to the hand-written cmd_submit shim.

    The submit-spec primitive's CLI adapter has branching that the
    auto-dispatcher cannot model: a manual required-field check + a
    dry-run path that emits a different envelope shape than the
    success path. The hand-written body lives in
    :mod:`hpc_agent.cli.submit`; this thunk wires it to the registry.
    """
    from hpc_agent.cli.submit import cmd_submit

    return cmd_submit(ns)


@primitive(
    name="submit-spec",
    verb="submit",
    side_effects=[
        SideEffect("writes-journal", "~/.claude/hpc/<repo_hash>/runs/<run_id>.json"),
        SideEffect("scheduler-submit", "<cluster>"),
    ],
    # ``SchedulerThrottled`` was declared phantom — nothing raises it;
    # throttling surfaces as ``RemoteCommandFailed``. Replaced.
    error_codes=[
        errors.SpecInvalid,
        errors.ClusterUnknown,
        errors.SshUnreachable,
        errors.RemoteCommandFailed,
    ],
    idempotent=True,
    idempotency_key="spec.run_id",
    cli=CliShape(
        help=(
            "Record a submission in the journal. Idempotent on run_id: "
            "the bundled atomic-ops layer dedups so a retry on transient "
            "network errors does not double-submit."
        ),
        verb="submit",
        requires_ssh=True,
        spec_arg=True,
        spec_model=None,
        spec_required=True,
        experiment_dir_arg=True,
        args=(
            CliArg(
                "--dry-run",
                action="store_true",
                help="Validate the spec and report what would be launched; no SSH/qsub.",
            ),
        ),
        handler=_submit_spec_handler,
    ),
    agent_facing=True,
)
def submit_and_record(
    experiment_dir: Path,
    *,
    spec: SubmitSpec,
    cmd_sha: str | None = None,
) -> tuple[RunRecord, bool]:
    """Build a fresh ``RunRecord`` and upsert it to the journal.

    The journal entry is keyed by *run_id* — the per-run sidecar at
    ``.hpc/runs/<run_id>.json`` is the source of truth for everything
    the cluster-side dispatcher and combiner consume; the journal record
    is the laptop-side bookkeeping that lets a future ``/status`` resume
    monitoring without re-asking the user for cluster / job_ids.

    *campaign_id* tags the run as part of a closed-loop campaign so
    :func:`hpc_agent.state.index.find_runs_by_campaign` can pick it up on resume.
    Defaults to an empty string for open-loop submits.

    Returns ``(record, deduped)`` where ``deduped`` is True if a record
    with this ``run_id`` already existed and the call was a no-op replay.
    Submissions are deterministic in ``run_id``, so a retry on transient
    network errors gets dedup for free — the cluster does not see
    duplicate ``qsub``/``sbatch`` calls because the caller checks the
    returned ``deduped`` flag before issuing them.
    """
    profile = spec.profile
    cluster = spec.cluster
    ssh_target = spec.ssh_target
    remote_path = spec.remote_path
    job_name = spec.job_name
    run_id = spec.run_id
    job_ids = list(spec.job_ids)
    total_tasks = spec.total_tasks
    campaign_id = spec.campaign_id or ""

    existing = load_run(experiment_dir, run_id)
    if existing is not None:
        return existing, True

    # A5: cmd_sha-based dedup. Covers the case where the journal at
    # ~/.claude/hpc/<repo_hash>/runs/ has been wiped (rm -rf, machine
    # swap) but the per-experiment sidecar at <exp>/.hpc/runs/<id>.json
    # still exists. Without this fallback, submit_and_record would
    # generate a fresh RunRecord and the caller would re-submit a job
    # the cluster already has running.
    if cmd_sha:
        sidecar_path = find_run_by_cmd_sha(experiment_dir, cmd_sha)
        if sidecar_path is not None:
            existing_run_id = sidecar_path.stem
            sidecar_data = None
            try:
                sidecar_data = read_run_sidecar(experiment_dir, existing_run_id)
            except (FileNotFoundError, OSError, json.JSONDecodeError):
                sidecar_data = None
            if sidecar_data is not None:
                # All sidecars produced by the framework are live records
                # we should dedup against — the journal lifecycle has no
                # "cancelled" status, so any historical guard for that
                # value was dead code.
                # ssh_target and job_name are NOT v2 sidecar fields (see
                # _V2_CONFIG_FIELDS in state/runs.py) — they live on the
                # journal RunRecord. The earlier sidecar.get(...) reads
                # for these always returned None and fell through to the
                # caller-supplied args, so the dict-reads were dead.
                reconstructed = RunRecord(
                    run_id=existing_run_id,
                    profile=str(sidecar_data.get("profile") or profile),
                    cluster=str(sidecar_data.get("cluster") or cluster),
                    ssh_target=ssh_target,
                    remote_path=str(sidecar_data.get("remote_path") or remote_path),
                    job_name=job_name,
                    job_ids=list(sidecar_data.get("job_ids") or []),
                    total_tasks=int(sidecar_data.get("task_count") or total_tasks),
                    submitted_at=str(sidecar_data.get("submitted_at") or utcnow_iso()),
                    experiment_dir=str(Path(experiment_dir).resolve()),
                    campaign_id=str(sidecar_data.get("campaign_id") or campaign_id),
                )
                # Repair the journal so future load_run calls hit it
                # directly without re-doing the cmd_sha scan.
                upsert_run(experiment_dir, reconstructed)
                return reconstructed, True

    record = RunRecord(
        run_id=run_id,
        profile=profile,
        cluster=cluster,
        ssh_target=ssh_target,
        remote_path=remote_path,
        job_name=job_name,
        job_ids=list(job_ids),
        total_tasks=int(total_tasks),
        submitted_at=utcnow_iso(),
        experiment_dir=str(Path(experiment_dir).resolve()),
        campaign_id=campaign_id,
    )
    upsert_run(experiment_dir, record)
    # Post-qsub finalize: stamp the per-experiment sidecar with the job_ids
    # we just got back. This is what distinguishes a real run from the
    # half-baked sidecar Step 6d of /submit-hpc writes before rsync — see
    # :func:`hpc_agent.state.runs.is_orphan_sidecar`. Best-effort: if the
    # sidecar isn't on disk yet (callers that skipped Step 6d), we don't
    # synthesize one — the journal record alone is enough to deflect the
    # orphan check.
    try:
        from hpc_agent.state.runs import update_run_sidecar_job_ids

        update_run_sidecar_job_ids(experiment_dir, run_id, list(job_ids))
    except FileNotFoundError:
        pass
    return record, False


def build_job_env(runtime_spec: dict[str, Any], base_env: dict[str, str]) -> dict[str, str]:
    """Return *base_env* augmented with runtime-derived env vars.

    *runtime_spec* is a small dict carrying any runtime selector the
    caller wants threaded into the cluster job — typically
    ``{"runtime": "uv"}`` taken from the submit-spec. When
    ``runtime_spec.get("runtime") == "uv"``, sets ``HPC_RUNTIME=uv`` so
    the cluster-side template's ``uv sync`` preamble fires. Any other
    value (or an empty dict) returns a plain copy of *base_env*. Never
    mutates either input.

    Add new branches as new runtime profiles land (``pixi``, ``poetry``,
    …); the contract — copy + augment — should stay invariant.
    """
    env = dict(base_env)
    if runtime_spec.get("runtime") == "uv":
        env["HPC_RUNTIME"] = "uv"
    return env
