"""Pydantic model for the ``build-submit-spec`` scaffold's input."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hpc_agent._wire._shared import (
    BackendName,
    CampaignId,
    RunIdStrict,
    Runtime,
    SshTarget,
)
from hpc_agent._wire.actions.write_run_sidecar import (
    _CONSTANT_PER_RUN_PLACEHOLDERS,
    _result_dir_per_task_placeholders,
)


class BuildSubmitSpecInput(BaseModel):
    """Resolved interview values fed to ``hpc_agent.incorporation.build.submit_spec``.

    The primitive synthesizes the framework-required job_env keys
    and emits a validated submit_flow.input.json spec ready for
    submit-flow.
    """

    model_config = ConfigDict(extra="forbid", title="build-submit-spec input")

    profile: str = Field(min_length=1)
    cluster: str = Field(min_length=1)
    ssh_target: SshTarget
    remote_path: str = Field(min_length=1)
    run_id: RunIdStrict
    # Lowercase-hex only (sha256.hexdigest produces lowercase). Width
    # 8-64 to match the canonical shape used by recall / interview /
    # validate_stochastic_marker — letting a recall lookup's 8-char
    # prefix thread through to build-submit-spec without hitting a wire
    # validation error (v3 BUG-3V3-3, unify cmd_sha regex across models).
    cmd_sha: str = Field(pattern=r"^[0-9a-f]{8,64}$")
    total_tasks: int = Field(ge=1)
    backend: BackendName
    is_gpu: bool | None = None
    job_name: str | None = None
    script: str | None = None
    modules: str | None = None
    conda_source: str | None = None
    conda_env: str | None = None
    runtime: Runtime | None = None
    campaign_id: CampaignId | None = None
    # Per-task result-dir template recorded on the sidecar; threading it
    # through here lets submit-flow guarantee the cluster-required sidecar
    # exists at rsync time (#148).
    result_dir_template: str | None = None
    # Scheduler resource asks emitted as qsub/sbatch flags (#146). All
    # opt-in; omitted fields leave the template directives / cluster
    # defaults in force.
    walltime_sec: int | None = Field(default=None, gt=0)
    mem_mb: int | None = Field(default=None, gt=0)
    cpus: int | None = Field(default=None, ge=1)
    canary: bool | None = None
    partial_ok: bool | None = None
    # ``skip_preflight`` removed (#275): build-submit-spec no longer emits it
    # onto the submit_flow spec, and an agent can't request it. The preflight
    # skip is operator-only (``HPC_AGENT_SKIP_PREFLIGHT=1``).
    # Opt-in code-iteration safety (#207). Threaded verbatim onto the
    # emitted submit_flow spec so an executor-body edit with unchanged
    # swept params forces a fresh run instead of a stale cmd_sha replay.
    # Null/false leaves the default param-only dedup key in force.
    invalidate_on_code_change: bool | None = None
    pass_env_keys: list[str] | None = None
    rsync_excludes: list[str] | None = None
    slurm_account: str | None = None
    slurm_cluster: str | None = None
    extra_env: dict[str, str] | None = None
    # Externally-provisioned companion-service address (#231 Tier 1). Travels
    # to the cluster job as the JSON ``HPC_SERVICE_ENV`` var; the dispatcher
    # threads each entry into every task's env as ``HPC_SERVICE_<KEY>``. The
    # framework does not stand the service up — it only consumes the address.
    service_env: dict[str, str] | None = None

    @model_validator(mode="after")
    def _per_task_result_dir_isolation(self) -> BuildSubmitSpecInput:
        """Refuse a ``result_dir_template`` that would render to the same
        path for every task when ``total_tasks > 1``.

        Mirrors the same guard on ``WriteRunSidecarInput`` (see that model
        for the empirical case). Catches the bad template one step earlier
        — at submit-spec build time, before the sidecar is even written —
        with the same per-task-placeholder rule.

        ``None`` passes through unchanged; the build site fills in a
        framework default (which itself includes ``{task_id}``).
        """
        if self.result_dir_template is None or self.total_tasks <= 1:
            return self
        per_task = _result_dir_per_task_placeholders(self.result_dir_template)
        if per_task:
            return self
        import re as _re

        all_placeholders = set(_re.findall(r"\{([^}]+)\}", self.result_dir_template))
        raise ValueError(
            f"result_dir_template={self.result_dir_template!r} has no per-task "
            f"placeholder, but total_tasks={self.total_tasks}. All tasks would "
            f"render to the same directory and clobber each other's output. "
            f"Found placeholders {sorted(all_placeholders) or 'none'}; only "
            f"{sorted(_CONSTANT_PER_RUN_PLACEHOLDERS & all_placeholders) or 'no'} "
            f"are constant per run. Add {{task_id}} for guaranteed uniqueness, "
            f"e.g. 'results/{{run_id}}/task_{{task_id}}', or use a swept kwarg "
            f"from tasks.py FLAGS such as 'results/{{run_id}}/seed_{{seed}}'."
        )
