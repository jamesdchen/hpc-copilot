"""Pydantic models for the ``submit-and-verify`` workflow primitive.

A composite workflow that chains ``submit-flow`` + ``verify-canary``:
submit a run plus its canary, then wait for the canary to land before
returning. One call replaces the two-step ``/submit-hpc`` then
``/verify-canary`` agent flow.

``SubmitAndVerifySpec`` embeds the existing :class:`SubmitFlowSpec`
under ``submit`` rather than redeclaring fields, so this workflow
inherits every submit-side knob automatically.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec
from hpc_agent._wire.workflows.verify_canary import (
    CanaryFailureKind,
    VerifyCanaryResult,
)


class SubmitAndVerifySpec(BaseModel):
    """Spec passed to ``hpc-agent submit-and-verify --spec <file>``."""

    model_config = ConfigDict(extra="forbid", title="submit-and-verify input spec")

    submit: SubmitFlowSpec = Field(
        description=(
            "The submit-flow spec the run will execute. Must have "
            "canary=True for verification to run; canary=False makes "
            "the workflow degenerate to a bare submit-flow call."
        ),
    )
    expect_output: str | None = Field(
        default=None,
        description=(
            "Path (relative to remote_path or absolute) the canary "
            "should have written. Forwarded to verify-canary; None "
            "skips the output-existence check."
        ),
    )
    fingerprint: str | None = Field(
        default=None,
        description=(
            "Relative path under the canary's result_dir of a file "
            "to SHA256 over SSH. Forwarded to verify-canary; None "
            "skips fingerprinting."
        ),
    )
    poll_interval_sec: int = Field(
        default=10,
        ge=1,
        description="Adaptive poll interval for the canary wait, in seconds.",
    )
    wait_budget_sec: int = Field(
        default=1800,
        ge=1,
        description=(
            "Total seconds to wait for the canary to land terminal "
            "before giving up with failure_kind='timeout'."
        ),
    )
    log_dir: str = Field(
        default="logs",
        description="Cluster-side log directory for the canary stderr scan.",
    )
    file_glob: str = Field(
        default="*",
        description="Cluster-side log file glob for the canary stderr scan.",
    )


class SubmitAndVerifyResult(BaseModel):
    """Shape of the ``data`` field on a successful envelope.

    Always carries the submit half; the verify half is None when the
    canary was skipped (``submit.canary=False``) or when the submit
    was a deduped replay (no fresh canary to wait on).
    """

    model_config = ConfigDict(extra="forbid", title="submit-and-verify output data")

    run_id: str = Field(description="Main run id (mirrors submit-flow's run_id).")
    job_ids: list[str] = Field(
        description="Main array job ids from submit-flow.",
    )
    total_tasks: int = Field(ge=1)
    deduped: bool = Field(
        description="True when the submit half was a deduped replay.",
    )
    canary_run_id: str | None = Field(
        default=None,
        description=(
            "Run id of the canary sibling sidecar. None when canary "
            "was skipped (submit.canary=False) or on a deduped replay."
        ),
    )
    canary_job_ids: list[str] | None = Field(
        default=None,
        description="Scheduler ids for the canary. None when canary skipped.",
    )
    verified: bool = Field(
        description=(
            "True iff verify-canary returned ok=True. False on any "
            "canary-side failure AND when canary verification was "
            "skipped (no canary fired)."
        ),
    )
    failure_kind: CanaryFailureKind | None = Field(
        default=None,
        description=(
            "Pass-through from verify-canary. None on success, None when canary was skipped."
        ),
    )
    verify_result: VerifyCanaryResult | None = Field(
        default=None,
        description=(
            "Full verify-canary envelope when verification ran; None "
            "when canary was skipped or submit was deduped."
        ),
    )
