"""Pydantic model for the ``write-run-sidecar`` primitive's input.

Mirrors the kwargs of :func:`hpc_agent.state.runs.write_run_sidecar`
(see `state/runs.py:184`) minus the two fields the primitive auto-stamps
(``submitted_at``, ``hpc_agent_version``) so the agent can write the
required sidecar via a single CLI invocation instead of an introspected
Python call (#200).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import (
    CampaignId,
    RunIdStrict,
    Runtime,
)


class WriteRunSidecarInput(BaseModel):
    """Resolved fields written into ``.hpc/runs/<run_id>.json``.

    All ``v2 config-snapshot`` fields are optional at the call site but
    every successful ``/submit`` should populate the ones that apply so
    downstream commands can rebuild full context without consulting any
    external config file (same convention as the underlying function's
    docstring).
    """

    model_config = ConfigDict(extra="forbid", title="write-run-sidecar input")

    # ----- required identity + cluster contract -----
    run_id: RunIdStrict
    # Same regex as build-submit-spec — accept the 8-char prefix that
    # threads through from a recall lookup, full 64-char hex too.
    cmd_sha: str = Field(pattern=r"^[0-9a-f]{8,64}$")
    # The REAL per-task command (e.g. ``python train.py --seed $SEED``).
    # NOT the job-script dispatcher command — that lives in
    # job_env["EXECUTOR"] on the submit-flow spec, not here. The
    # #162 check refuses dispatcher-shaped values on the sidecar's
    # executor field; the primitive surfaces that as a SpecInvalid.
    executor: str = Field(min_length=1)
    result_dir_template: str = Field(min_length=1)
    task_count: int = Field(ge=1)
    # SHA of the on-disk tasks.py. Empty string disables the drift guard
    # (the dispatcher silently no-ops on '') — matches the function's
    # opt-in semantics.
    tasks_py_sha: str = Field(default="", pattern=r"^([0-9a-f]{64})?$")

    # ----- optional wave + extras -----
    wave_map: dict[str, list[int]] | None = None
    extra: dict[str, Any] | None = None

    # ----- v2 config-snapshot fields (all optional) -----
    cluster: str | None = None
    profile: str | None = None
    campaign_id: CampaignId | None = None
    project: str | None = None
    remote_path: str | None = None
    resources: dict[str, Any] | None = None
    env: dict[str, Any] | None = None
    env_group: str | None = None
    constraints: dict[str, Any] | None = None
    gpu_fallback: list[str] | None = None
    max_retries: int | None = Field(default=None, ge=0)
    runtime: Runtime | None = None
    auto_retry: dict[str, Any] | None = None
    aggregate_defaults: dict[str, Any] | None = None
    results: dict[str, Any] | None = None
    job_ids: list[str] | None = None
