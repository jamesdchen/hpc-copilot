"""Pydantic model for the ``build-submit-spec`` scaffold's input."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import (
    BackendName,
    CampaignId,
    RunIdStrict,
    Runtime,
    SshTarget,
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
    skip_preflight: bool | None = None
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
