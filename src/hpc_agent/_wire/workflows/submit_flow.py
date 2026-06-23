"""Pydantic models for the ``submit-flow`` workflow atom's wire contract.

These author ``schemas/submit_flow.input.json`` and
``schemas/submit_flow.output.json`` via
``scripts/build_schemas.py``. The atom signature itself
(``ops/submit_flow.py``) is still keyword-arg + frozen-dataclass
today; switching to consume ``SubmitFlowSpec`` directly is a
follow-up to this canary.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hpc_agent._wire._shared import (
    BackendName,
    CampaignId,
    RunIdStrict,
    Runtime,
    SshTarget,
)

# Launchers the MPI template knows how to wrap an executor with. ``srun``
# (SLURM-native), ``mpirun`` (OpenMPI / Intel MPI / MPICH), ``aprun`` (Cray
# ALPS). The set is closed on purpose — a launcher the template has no
# ``case`` arm for would silently run the bare executor on one rank.
MpiLauncher = Literal["srun", "mpirun", "aprun"]


class MpiSpec(BaseModel):
    """Multi-rank job shape: N ranks across M nodes as ONE unit of work (#293).

    hpc-agent's default axis is independent-task fan-out (one task = one
    process). An MPI solve breaks that shape — a single computation spans
    ``ranks`` processes that coordinate via the MPI library, and the whole
    multi-rank job is one unit of work. This block, hung on
    :class:`SubmitResources`, carries the rank/topology controls; the
    backend's ``resource_flags`` translates them into scheduler directives
    (``--ntasks`` / ``-pe`` / ``select=``) and the ``mpi`` job template wraps
    the executor in the chosen ``launcher``.

    Every field but ``ranks`` is optional. ``ranks_per_node`` left null lets
    the scheduler pack ranks onto nodes; set it to pin the decomposition
    (then ``nodes = ranks / ranks_per_node`` and the divisibility guard
    below fires on an incoherent combo).
    """

    model_config = ConfigDict(extra="forbid", title="mpi spec")

    ranks: int = Field(
        ge=1,
        description="Total MPI ranks (processes) for the job. SLURM --ntasks / SGE -pe <pe> N.",
    )
    ranks_per_node: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Ranks packed onto each node. Null lets the scheduler decide. When "
            "set, must divide ``ranks`` evenly so node count is integral."
        ),
    )
    threads_per_rank: int = Field(
        default=1,
        ge=1,
        description="OpenMP threads per rank (hybrid MPI+OpenMP). SLURM --cpus-per-task.",
    )
    launcher: MpiLauncher = Field(
        description="How the job template launches the ranks: srun / mpirun / aprun.",
    )
    pe_name: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "SGE parallel environment to request (``-pe <pe_name> <ranks>``). "
            "Resolved from inspect-cluster's parallel_environments (kind=mpi). "
            "Required for SGE; ignored by SLURM/PBS."
        ),
    )

    @model_validator(mode="after")
    def _ranks_per_node_divides_ranks(self) -> MpiSpec:
        """Refuse ``ranks_per_node`` that does not evenly divide ``ranks``.

        The #293 coherence guard: ``ranks_per_node × nodes`` must equal
        ``ranks``. A non-dividing pair (e.g. 6 ranks, 4 per node) has no
        integral node count — the scheduler would either reject it or
        silently round, stranding ranks. Caught here so build-submit-spec
        refuses it before qsub rather than after a wasted submission.
        """
        rpn = self.ranks_per_node
        if rpn is not None and self.ranks % rpn != 0:
            raise ValueError(
                f"ranks_per_node={rpn} does not evenly divide ranks={self.ranks}: "
                f"node count {self.ranks / rpn} is not integral. Pick a "
                f"ranks_per_node that divides {self.ranks} (e.g. a factor of it), "
                "or leave it null to let the scheduler pack ranks onto nodes."
            )
        return self


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
    mpi: MpiSpec | None = Field(
        default=None,
        description=(
            "Multi-rank (MPI) job shape (#293). When set, the job is ONE "
            "multi-rank unit of work rather than a fan-out of single-process "
            "tasks: the backend emits multi-node directives (--ntasks / -pe / "
            "select=) and the mpi template wraps the executor in the launcher. "
            "Null = ordinary single-process task (cpus/mem/walltime apply as "
            "before)."
        ),
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
            "Null (or omit) = forward every key in job_env; pass a list to "
            "restrict to those keys. An EMPTY list is refused — it would "
            "forward zero vars and produce a broken job. Ignored for SLURM "
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
    scheduler_profile: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Pinned SchedulerProfile (as a dict) for a cluster whose scheduler "
            "differs from the golden slurm/sge defaults. When set, the backend "
            "is built bound to this profile: its 'family' (slurm/sge) selects "
            "the command grammar and its data (job_id_regex, scripts, "
            "error_states) overrides the golden default. Null = use the golden "
            "profile for 'backend'. Typically sourced from the cluster's "
            "clusters.yaml 'scheduler_profile' entry."
        ),
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
    canary_skip_threshold: int = Field(
        default=4,
        ge=0,
        description=(
            "Auto-skip the canary when total_tasks <= this threshold (#263): for "
            "a tiny batch the main array's own first tasks catch a broken "
            "executor as fast as a canary would, so the canary is pure friction. "
            "Default 4. Set 0 to always canary. Env HPC_CANARY_SKIP_THRESHOLD "
            "overrides per-invocation. Ignored when canary_only=true (an explicit "
            "two-phase gate) or force_canary=true."
        ),
    )
    force_canary: bool = Field(
        default=False,
        description=(
            "Always submit a canary, overriding both the total_tasks<=threshold "
            "auto-skip (#263) and the cached-cmd_sha skip (#249). For the rare "
            "small-but-expensive batch where the extra safety is worth it."
        ),
    )
    enable_afterok_dependency: bool = Field(
        default=False,
        description=(
            "Gate the main array on the canary SUCCEEDING via a scheduler-level "
            "afterok dependency (#250) instead of the canary running independently "
            "of main. When the canary fires AND the scheduler supports afterok "
            "(SLURM / PBS; SGE has no native afterok and is left un-gated), the "
            "main array is submitted immediately holding on afterok:<canary_job_id> "
            "— the scheduler drops main if the canary fails — so there is no "
            "orchestrator wait+verify+resubmit round-trip. Opt-in (default off)."
        ),
    )
    campaign_id: CampaignId | None = Field(default=None)
    parents: list[RunIdStrict] | None = Field(
        default=None,
        min_length=1,
        description=(
            "Run_ids whose outputs this run consumes (DAG lineage, "
            "docs/design/dag-kernel.md). Each must have a local sidecar "
            "under .hpc/runs/. The framework records them as "
            "parent_run_ids on this run's sidecar and derives node_sha = "
            "compose_node_sha(cmd_sha, parent identities) so dedup keys on "
            "params AND ancestry — a parent re-run with changed params can "
            "never silently replay this child's stale results. Opaque "
            "lineage only: the framework hands paths across the edge "
            "(parent_records()), never interprets them. Readiness is NOT "
            "checked by submit-flow itself — submit-pipeline composes "
            "validate-parents-ready mechanically when parents is set; bare "
            "submit-flow callers compose it themselves. Callers that need "
            "the lineage cluster-side forward it via job_env "
            "(HPC_PARENT_RUN_IDS), same convention as HPC_CAMPAIGN_ID."
        ),
    )
    runtime: Runtime | None = Field(default=None)
    input_datasets: list[str] | None = Field(
        default=None,
        min_length=1,
        description=(
            "Declared input-dataset path(s) for DATA provenance (#222/#312) — "
            "the same path(s) a validate-input-dataset gate names as "
            "dataset_path. When set, submit-flow auto-captures data_sha = "
            "compute_data_sha(input_datasets) on this run's sidecar at "
            "sidecar-write time, exactly as env_hash is auto-captured — no "
            "manual write-run-sidecar step. Relative paths resolve against "
            "the experiment dir; a DVC-tracked path uses the .dvc pointer's "
            "recorded md5; a declared-but-missing path contributes the "
            "'absent' sentinel (the absence IS the provenance fact). When "
            "unset, data_sha stays null — 'not captured' is distinguishable "
            "from any real digest. Provenance only: never part of the dedup "
            "identity."
        ),
    )
    auto_resume_on_kill: bool = Field(
        default=False,
        description=(
            "Opt-in automatic checkpoint-resume on a preemption/walltime kill "
            "(#294 Layer 2 / #299). Default OFF: a run that does not set this is "
            "NEVER auto-resubmitted. When ON, the monitor's terminal-FAILED hook "
            "consults the auto-resume gate — and ONLY on an explicit per-task "
            "preempt mark (the dispatcher's SIGTERM signal; exit 130). OOM (137) "
            "and executor errors carry no mark and always escalate instead of "
            "looping. Requires checkpoint-writing executors to make progress; a "
            "task with no checkpoint just restarts from scratch on resume."
        ),
    )
    max_auto_resumes: int = Field(
        default=2,
        ge=1,
        description=(
            "Hard cap on automatic resumes for this run when auto_resume_on_kill "
            "is set (#299). The ultimate backstop: even total misclassification "
            "can waste at most this many resubmits before the gate escalates with "
            "'cap reached'. Ignored when auto_resume_on_kill is false."
        ),
    )
    auto_recover_on_failure: bool = Field(
        default=False,
        description=(
            "Opt-in automatic deterministic recovery on a non-preempt FAILED "
            "tick (#240 / #234). Default OFF: a run that does not set this is "
            "NEVER auto-recovered — the monitor's resolve-and-recover hook still "
            "computes and surfaces the verdict-as-data (#283) but takes no side "
            'effect. When ON, a ``decided_by="code"`` verdict under the cap is '
            "auto-resubmitted with the resolver's refined overrides; a "
            '``decided_by="judgement"`` verdict is always parked, never '
            "auto-acted. Independent of auto_resume_on_kill (which stays "
            "preempt-only) — enabling general auto-recovery is a deliberate "
            "separate choice."
        ),
    )
    max_auto_recovers: int = Field(
        default=2,
        ge=1,
        description=(
            "Hard cap on automatic code-verdict resubmits for this run when "
            "auto_recover_on_failure is set (#240). The ultimate backstop: even "
            "total misclassification can waste at most this many resubmits before "
            "the composite parks with 'cap reached'. Ignored when "
            "auto_recover_on_failure is false."
        ),
    )
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
    # ``skip_preflight`` was removed from this wire surface (#275). It was an
    # agent-settable field whose SKILL.md example told agents to set it
    # ``true`` — which silenced submit-flow's ``command -v uv`` runtime probe
    # and launched arrays doomed by ``HPC_RUNTIME=uv but 'uv' not on PATH``.
    # The skip is now operator-only: ``HPC_AGENT_SKIP_PREFLIGHT=1`` in the
    # environment, or a Python-only ``_skip_preflight`` kwarg for trusted
    # internal callers (submit_and_verify's post-canary main launch). Same
    # operator-vs-agent boundary as ``--inline`` / ``HPC_AGENT_INVOKER`` (#155).
    # ``extra="forbid"`` now refuses a stray ``skip_preflight`` outright.
    # ``skip_rsync_deploy`` was removed from this wire surface (#283, instance
    # #2). It was an agent-settable field whose ``submit.md`` Phase-2 example
    # taught agents to set it ``true`` — which dropped submit-flow's
    # rsync+deploy arm and launched the main array against whatever code the
    # PREVIOUS deploy shipped. On the legitimate path that is structurally
    # safe: the two-phase canary gate's in-process main-array launch
    # (``submit_and_verify``) skips the redundant rsync because Phase 1 just
    # deployed the SAME tree moments earlier — "Phase 1 just deployed" is a
    # fact the code knows, not an assertion the agent makes. A hand-authored
    # ``skip_rsync_deploy: true`` on a raw submit-flow spec is the bug surface:
    # the agent ASSERTS "nothing changed since the last deploy," and a stale
    # assertion silently runs the cluster on old code (#185).
    #
    # The skip is now operator/internal-only, mirroring ``skip_preflight``
    # (#275) and ``--inline`` / ``HPC_AGENT_INVOKER`` (#155):
    # ``HPC_AGENT_SKIP_RSYNC_DEPLOY=1`` in the environment, or a Python-only
    # ``_skip_rsync_deploy`` kwarg for trusted internal callers
    # (submit_and_verify's post-canary main launch). ``extra="forbid"`` now
    # refuses a stray ``skip_rsync_deploy`` outright.
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
    invalidate_on_code_change: bool = Field(
        default=False,
        description=(
            "Opt-in code-iteration safety (#207). cmd_sha (the dedup key, "
            "carried in job_env['HPC_CMD_SHA']) is PARAMETER identity only: "
            "editing the executor body without changing any swept parameter "
            "keeps the same cmd_sha, so a cross-machine resubmit (journal "
            "wiped, sidecar surviving) would dedup against — and silently "
            "replay — the prior run's OLD code. When true, the run's tasks.py "
            "drift sha (already recorded on the sidecar as tasks_py_sha) is "
            "folded into the cmd_sha dedup fallback so a code-only change "
            "forces a fresh run. Default false leaves the param-only dedup "
            "key untouched; a detected drift still warns regardless of this "
            "flag. Threads through to submit_and_record."
        ),
    )

    @model_validator(mode="after")
    def _canary_only_requires_canary(self) -> SubmitFlowSpec:
        if self.canary_only and not self.canary:
            raise ValueError("canary_only=true requires canary=true (nothing to gate on otherwise)")
        return self

    @model_validator(mode="after")
    def _no_empty_pass_env_keys(self) -> SubmitFlowSpec:
        # `[]` is the natural-feeling JSON default for "no override", but it is
        # the WORST interpretation here: it forwards zero vars to qsub -v, so
        # every $EXECUTOR/$CONDA_ENV/$REPO_DIR is unset on the cluster and the
        # job runs `time ""` and exits 0 in milliseconds (#192). "Forward all"
        # is spelled `null`/omit, not `[]`. Refuse the empty list at intake with
        # an actionable message rather than let it sail to a vanished canary.
        if self.pass_env_keys is not None and len(self.pass_env_keys) == 0:
            raise ValueError(
                "pass_env_keys=[] forwards zero env vars to qsub and produces a "
                "broken job (every $EXECUTOR / $CONDA_ENV / $REPO_DIR unset, so the "
                'cluster runs `time ""` and exits 0 instantly). Omit the field '
                "(or pass null) to forward ALL keys from job_env; pass a non-empty "
                "list to restrict to those keys."
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
    run_id: RunIdStrict
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
