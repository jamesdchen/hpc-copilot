"""Pydantic models for the ``submit-flow-batch`` workflow atom's wire contract.

Note: per the original schema's design intent, the wrapper only
constrains the outer shape — each inner spec is validated separately
by the CLI loading ``submit_flow.input.json``. So the inner item
model uses ``extra="allow"`` and lists only the required fields
(no ``additionalProperties: false``).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ._shared import RunIdLoose


class _SubmitFlowSpecOuter(BaseModel):
    """Outer-shape only validation for one submit-flow spec inside a batch."""

    model_config = ConfigDict(extra="allow")

    profile: str
    cluster: str
    ssh_target: str
    remote_path: str
    run_id: str
    total_tasks: int
    backend: str
    job_name: str


class SubmitFlowBatchSpec(BaseModel):
    """Wraps a JSON list of submit-flow specs that share ``(ssh_target, remote_path)``.

    The batch atom does ONE rsync_push + ONE deploy_runtime + N
    qsubs (multiplexed via the ssh ControlMaster) instead of N times
    the per-spec submit-flow pipeline. Use whenever a campaign
    iteration or multi-executor /submit-hpc emits >1 specs to the
    same cluster; heterogeneous (different ssh_target or remote_path)
    batches raise spec_invalid. Each list element under ``specs``
    MUST individually validate against submit_flow.input.json (the
    CLI loads that schema and validates per-entry); this wrapper
    schema only constrains the outer shape so an agent or external
    orchestrator can sanity-check the bundle without doing the
    per-entry validation itself.
    """

    model_config = ConfigDict(title="submit-flow-batch input spec")

    specs: list[_SubmitFlowSpecOuter] = Field(min_length=1)
    rsync_excludes: list[str] | None = Field(
        default=None,
        description="Optional rsync exclude patterns applied once across the bundle.",
    )
    skip_preflight: bool | None = Field(
        default=None,
        description="Skip the single ssh probe; default false.",
    )


class _SubmitFlowResultEntry(BaseModel):
    """One per-spec submit-flow result inside a batch envelope.

    Mirrors submit_flow.output.json (same fields). Kept as a separate
    model so the batch wrapper schema doesn't reference
    ``submit_flow.output.json#`` cross-file — single self-contained file.
    """

    run_id: RunIdLoose
    job_ids: list[str]
    total_tasks: int = Field(ge=1)
    deduped: bool
    canary_done: bool
    canary_run_id: str | None = None
    canary_job_ids: list[str] | None = None


class SubmitFlowBatchResult(BaseModel):
    """Shape of the ``data`` field on a successful ``submit-flow-batch`` envelope.

    Wraps a list of per-spec result records (each matching
    submit_flow.output.json) plus a count for cheap iteration. Order
    of ``results`` matches the order of input ``specs``.
    """

    model_config = ConfigDict(extra="forbid", title="submit-flow-batch output data")

    results: list[_SubmitFlowResultEntry] = Field(
        min_length=1,
        description=(
            "Per-spec submit-flow result, in input order. Each entry "
            "has the shape of submit_flow.output.json (run_id, "
            "job_ids, total_tasks, deduped, canary_done, "
            "canary_run_id, canary_job_ids)."
        ),
    )
    n_results: int = Field(
        ge=1,
        description=(
            "Length of `results`. Equals the number of input specs "
            "(deduped specs still contribute one result entry with "
            "deduped=true)."
        ),
    )
