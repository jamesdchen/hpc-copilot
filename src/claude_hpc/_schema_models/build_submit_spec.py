"""Pydantic model for the ``build-submit-spec`` scaffold's input."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ._shared import BackendName, RunIdStrict


class BuildSubmitSpecInput(BaseModel):
    """Resolved interview values fed to ``claude_hpc.atoms.build_submit_spec``.

    The primitive synthesizes the framework-required job_env keys
    and emits a validated submit_flow.input.json spec ready for
    submit-flow.
    """

    model_config = ConfigDict(extra="forbid", title="build-submit-spec input")

    profile: str = Field(min_length=1)
    cluster: str = Field(min_length=1)
    ssh_target: str = Field(min_length=1)
    remote_path: str = Field(min_length=1)
    run_id: RunIdStrict
    cmd_sha: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    total_tasks: int = Field(ge=1)
    backend: BackendName
    is_gpu: bool | None = None
    job_name: str | None = None
    script: str | None = None
    modules: str | None = None
    conda_source: str | None = None
    conda_env: str | None = None
    runtime: str | None = None
    campaign_id: str | None = None
    canary: bool | None = None
    partial_ok: bool | None = None
    skip_preflight: bool | None = None
    pass_env_keys: list[str] | None = None
    rsync_excludes: list[str] | None = None
    slurm_account: str | None = None
    slurm_cluster: str | None = None
    extra_env: dict[str, str] | None = None
