"""Pydantic models for the ``submit-flow`` workflow atom's wire contract.

These author ``schemas/submit_flow.input.json`` and
``schemas/submit_flow.output.json`` via
``scripts/build_schemas.py``. The atom signature itself
(``ops/submit_flow.py``) is still keyword-arg + frozen-dataclass
today; switching to consume ``SubmitFlowSpec`` directly is a
follow-up to this canary.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hpc_agent._wire._shared import (
    BackendName,
    CampaignId,
    RunIdLoose,
    RunIdStrict,
    Runtime,
    SshTarget,
)


class SubmitResources(BaseModel):
    """Scheduler resource asks emitted as qsub/sbatch flags.

    First-class in the submit spec (#146): the planning/validation layer
    already resolves and validates walltime against history + cluster
    ceilings, but before this the submission layer had nowhere to put the
    result, so the resolved walltime was silently dropped and the job ran
    on the cluster default. Every field is optional and opt-in — an
    omitted/empty ``resources`` block emits NO new scheduler flags, so the
    template directives (and the cluster default) apply exactly as before.

    The backend translates each set field into its scheduler's flag:

    * ``walltime_sec`` → SGE ``-l h_rt=HH:MM:SS`` / SLURM ``--time=<min>``
    * ``mem_mb``       → SGE ``-l h_data=<mem>M`` / SLURM ``--mem=<mem>M``
    * ``cpus``         → SGE ``-pe shared <n>`` / SLURM ``--cpus-per-task=<n>``

    These override the corresponding directive baked into the job
    template (a command-line flag beats a ``#$``/``#SBATCH`` line), which
    is the only way to vary a per-submission resource since SGE ``#$``
    directives cannot read env vars.
    """

    model_config = ConfigDict(extra="forbid", title="submit resources")

    walltime_sec: int | None = Field(
        default=None,
        gt=0,
        description="Wall-clock limit in seconds. SGE -l h_rt / SLURM --time.",
    )
    mem_mb: int | None = Field(
        default=None,
        gt=0,
        description="Memory ask in MB. SGE -l h_data (per-slot) / SLURM --mem.",
    )
    cpus: int | None = Field(
        default=None,
        ge=1,
        description="CPU cores. SGE -pe shared <n> / SLURM --cpus-per-task.",
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
    canary_only: bool = Field(
        default=False,
        description=(
            "Two-phase canary gate (#160): submit ONLY the canary, do NOT "
            "launch the main array, and return main_launched=false. The caller "
            "then verifies the canary (hpc-agent verify-canary) and re-invokes "
            "submit-flow with canary=false to launch the main array only on "
            "success — so a broken dispatch can't sail past the canary into the "
            "full run. Requires canary=true."
        ),
    )
    campaign_id: CampaignId | None = Field(default=None)
    runtime: Runtime | None = Field(default=None)
    resources: SubmitResources | None = Field(
        default=None,
        description=(
            "Scheduler resource asks (walltime/mem/cpus) emitted as "
            "qsub/sbatch flags. Null/empty = no resource flags; the job "
            "template directives and cluster defaults apply unchanged."
        ),
    )
    result_dir_template: str | None = Field(
        default=None,
        description=(
            "Per-task result-dir template (e.g. 'results/{run_id}/task_{task_id}'). "
            "The cluster dispatcher hard-requires this (it reads it from the "
            "per-run sidecar). Supplying it lets submit-flow GUARANTEE the "
            "sidecar exists at rsync time — it synthesizes the sidecar from "
            "the spec when a prior step (write_run_sidecar / Step 6d) did not "
            "already write one, instead of shipping an empty .hpc/runs/ that "
            "dooms every cluster task. Null = rely on a pre-written sidecar."
        ),
    )
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

    @model_validator(mode="after")
    def _canary_only_requires_canary(self) -> SubmitFlowSpec:
        if self.canary_only and not self.canary:
            raise ValueError(
                "canary_only=true requires canary=true (nothing to gate on otherwise)"
            )
        return self


class SubmitFlowResult(BaseModel):
    """Shape of the ``data`` field on a successful ``submit-flow`` envelope."""

    model_config = ConfigDict(
        extra="forbid",
        title="submit-flow output data",
    )

    # Output uses the loose run_id form (any string) so legacy
    # sidecars validate.
    run_id: RunIdLoose
    job_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Scheduler IDs for the MAIN array. Empty when main_launched=false "
            "(the canary-only gating phase): verify the canary, then re-invoke "
            "submit-flow with canary=false to launch the main array."
        ),
    )
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
            "True when a 1-task canary was *submitted* (not yet verified) — "
            "verification is the caller's verify-canary step. False when the "
            "canary was skipped via spec.canary=false or on a deduped replay. "
            "Gate the main launch on verify-canary + main_launched, not on this."
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
    main_launched: bool = Field(
        default=True,
        description=(
            "True when the main array was submitted this call. False in the "
            "canary-only gating phase (#160): only the canary went out; the "
            "caller must verify it and re-invoke to launch the main array."
        ),
    )
