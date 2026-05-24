"""Pydantic models for the ``submit-flow`` workflow atom's wire contract.

These author ``schemas/submit_flow.input.json`` and
``schemas/submit_flow.output.json`` via
``scripts/build_schemas.py``. The atom signature itself
(``ops/submit/flow.py``) is still keyword-arg + frozen-dataclass
today; switching to consume ``SubmitFlowSpec`` directly is a
follow-up to this canary.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import (
    BackendName,
    CampaignId,
    RunIdLoose,
    RunIdStrict,
    Runtime,
    SshTarget,
)


class SubmitFlowSpec(BaseModel):
    """Spec passed to ``hpc-agent submit-flow --spec <file>``.

    Workflow atom that does pre-flight + rsync + deploy + optional
    canary + qsub + record in one shot. All judgment (which
    constraint, which walltime, which executor, scaffold tasks.py)
    is the caller's responsibility — this atom takes resolved values
    and executes.
    """

    model_config = ConfigDict(
        extra="forbid",
        # The hand-authored JSON titles the schema "submit-flow input
        # spec"; mirror that so the diff against the existing file
        # stays minimal.
        title="submit-flow input spec",
    )

    profile: str = Field(min_length=1)
    cluster: str = Field(min_length=1)
    ssh_target: SshTarget
    remote_path: str = Field(min_length=1)
    job_name: str = Field(min_length=1)
    run_id: RunIdStrict
    total_tasks: int = Field(ge=1)
    backend: BackendName
    script: str = Field(
        description=(
            "Path to the job script ON THE CLUSTER (e.g. .hpc/templates/"
            "cpu_array.sh). deploy_runtime places these under "
            "remote_path/.hpc/templates/."
        ),
    )
    job_env: dict[str, str] = Field(
        description=(
            "Env vars forwarded into the cluster job. Caller is "
            "responsible for setting EXECUTOR, HPC_RUN_ID, "
            "HPC_CMD_SHA, HPC_TASK_COUNT, REPO_DIR, MODULES, "
            "CONDA_SOURCE/CONDA_ENV (as needed), HPC_RUNTIME (if uv), "
            "HPC_CAMPAIGN_ID (if part of a campaign)."
        ),
    )

    # Optional fields below. Pydantic emits these with the right
    # ``["array", "null"]`` / ``["string", "null"]`` shape when the
    # type union includes None.
    pass_env_keys: list[str] | None = Field(
        default=None,
        description=(
            "SGE-only: which job_env keys to forward via qsub -v. "
            "Null = forward every key in job_env. Ignored for SLURM "
            "(slurm forwards everything in job_env automatically via "
            "--export ALL,...)."
        ),
    )
    slurm_account: str | None = Field(
        default=None,
        description=(
            "SLURM-only: account to charge (sbatch --account). "
            "Defaults to whatever SLURM picks for the user."
        ),
    )
    slurm_cluster: str | None = Field(
        default=None,
        description=(
            "SLURM-only: cluster name for federated SLURM "
            "installations (sbatch --clusters=). Most installs don't "
            "need this."
        ),
    )
    tasks_per_array: int | None = Field(
        default=None,
        ge=1,
        description=("Batch tasks into arrays of this size. Null = single array of total_tasks."),
    )
    canary: bool = Field(
        default=True,
        description=(
            "Submit a 1-task canary first; abort if it fails. Skip "
            "when caller has just smoke-tested or knows the pipeline "
            "is good."
        ),
    )
    campaign_id: CampaignId | None = Field(default=None)
    runtime: Runtime | None = Field(default=None)
    rsync_excludes: list[str] | None = Field(
        default=None,
        description="Override DEFAULT_RSYNC_EXCLUDES. Null uses defaults.",
    )
    skip_preflight: bool = Field(
        default=False,
        description=(
            "Skip the pre-flight check (caller has just run it; "
            "saves one SSH probe). Use with caution."
        ),
    )
    partial_ok: bool = Field(
        default=False,
        description=(
            "When true, the sidecar records partial_ok=True under "
            "the `extra` pocket. monitor-flow consults this on "
            "terminal classification: a wave with at least one "
            "success is reported as `complete` (instead of `failed`) "
            "when partial_ok=true, and a `<run_id>.failed.json` "
            "ledger lists the failed task IDs. aggregate-flow honors "
            "the same ledger by skipping those task IDs and "
            "reporting `partial_failures`."
        ),
    )


class SubmitFlowResult(BaseModel):
    """Shape of the ``data`` field on a successful ``submit-flow`` envelope."""

    model_config = ConfigDict(
        extra="forbid",
        title="submit-flow output data",
    )

    # Output uses the loose run_id form (any string) so legacy
    # sidecars validate.
    run_id: RunIdLoose
    job_ids: list[str] = Field(min_length=1)
    total_tasks: int = Field(ge=1)
    deduped: bool = Field(
        description=(
            "True when a journal record for run_id already existed "
            "and the call was a no-op replay. The original cluster "
            "jobs are running; do NOT re-issue qsub. Same semantics "
            "as submit-spec.deduped."
        ),
    )
    canary_done: bool = Field(
        description=(
            "True when a 1-task canary was submitted and verified "
            "before the main array. False when canary was skipped "
            "via spec.canary=false or when this is a deduped replay."
        ),
    )
    canary_run_id: str | None = Field(
        default=None,
        description=(
            "Run ID of the canary submission (a sibling sidecar). Null when canary skipped."
        ),
    )
    canary_job_ids: list[str] | None = Field(
        default=None,
        description="Scheduler IDs for the canary. Null when canary skipped.",
    )
