"""Pydantic models for the ``submit-flow-batch`` workflow atom's wire contract.

The wrapper's ``specs`` items are full :class:`SubmitFlowSpec`
models — the same canonical type the standalone ``submit-flow``
atom takes — so the wrapper schema strictly validates every inner
field instead of just the required-key set. The CLI handler also
runs ``_validate_against_schema(entry, "submit_flow")`` per entry
for diagnostic-quality error messages, but the structural contract
is now enforced by the wrapper alone.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import RunIdStrict

from .submit_flow import SubmitFlowSpec


class SubmitFlowBatchSpec(BaseModel):
    """Wraps a JSON list of submit-flow specs that share ``(ssh_target, remote_path)``.

    The batch atom does ONE rsync_push + ONE deploy_runtime + N
    qsubs (multiplexed via the ssh ControlMaster) instead of N times
    the per-spec submit-flow pipeline. Use whenever a campaign
    iteration or multi-executor /submit-hpc emits >1 specs to the
    same cluster; heterogeneous (different ssh_target or remote_path)
    batches raise spec_invalid. Each list element under ``specs`` is
    a full :class:`SubmitFlowSpec` — same wire shape as the
    standalone ``submit-flow`` atom takes.
    """

    model_config = ConfigDict(extra="forbid", title="submit-flow-batch input spec")

    specs: list[SubmitFlowSpec] = Field(min_length=1)
    rsync_excludes: list[str] | None = Field(
        default=None,
        description="Optional rsync exclude patterns applied once across the bundle.",
    )
    # ``skip_preflight`` was removed here too (#275) — see the note on
    # ``SubmitFlowSpec``. The skip is operator-only now
    # (``HPC_AGENT_SKIP_PREFLIGHT=1`` / the internal ``_skip_preflight`` kwarg on
    # ``submit_flow_batch``); an agent can no longer silence the preflight by
    # setting a bundle-level flag. ``extra="forbid"`` refuses a stray one.


class _SubmitFlowResultEntry(BaseModel):
    """One per-spec submit-flow result inside a batch envelope.

    Mirrors submit_flow.output.json (same fields). Kept as a separate
    model so the batch wrapper schema doesn't reference
    ``submit_flow.output.json#`` cross-file — single self-contained file.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: RunIdStrict
    job_ids: list[str] = Field(min_length=1)
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
