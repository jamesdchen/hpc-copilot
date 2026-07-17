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

from typing import Literal

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
    checkpoint_result_dir: str | None = Field(
        default=None,
        description=(
            "Checkpoint canary (#294 PR4) override: the canary task-0 result dir "
            "(relative to remote_path or absolute) whose _checkpoints/ the "
            "round-trip probe inspects. Only consulted when submit.auto_resume_on_kill "
            "is true (which auto-enables checkpoint verification). None derives it "
            "from the canary sidecar's result_dir_template — pass it explicitly only "
            "when that template references per-task kwargs that can't be rendered "
            "locally."
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


class ReducerCheckResult(BaseModel):
    """Rung-2 reducibility-ladder outcome: EXECUTING the run's declared custom
    reducer against the verified canary's ONE real task-0 row, before the main
    array launches (``docs/plans/amortized-reduction-check-2026-07-17.md``).

    Rung 1 (S1) is a STATIC predicate — "is a reducer *declared*?"; this rung is
    a DYNAMIC proof — "does the declared reducer *execute*?" — running the SAME
    ``cluster_reduce`` the final harvest runs (one-definition), so a broken
    reducer (py3.8-vs-3.13, a missing import, a wrong output path, non-JSON
    output) is caught NOW at zero critical-path wall-clock instead of hours later
    mid-harvest after the whole array computed. It asserts only the reducer's
    contract SHAPE (exit 0, parseable JSON, top-level keys) against one row —
    never a VALUE (a single canary row's aggregate number is meaningless;
    correctness is the aggregate stage's job).

    ``status``:

    * ``passed`` — the reducer exited 0 and emitted parseable JSON.
    * ``disclosed`` — the reducer RAN and produced positive evidence of a problem
      (non-zero exit / missing output / non-JSON). A LOUD, never-auto-masked
      disclosure on the S2/S3 brief — NEVER a hard block: the human's bare ``y``
      still crosses it (the failure MIGHT be a benign "needs ≥2 rows" false alarm
      a single canary row cannot satisfy — the machinery surfaces the verbatim
      stderr and stops, it never interprets "broken code" vs "needs more rows").
    * ``unverified`` — the check could not COMPLETE (ssh severed / breaker open /
      timeout). UNKNOWN, never reported as passed (positive-evidence-only, the
      same posture as ``reporter_unreachable`` and the combiner truncation rule).
    * ``skipped`` — the run declares no custom ``aggregate_cmd`` (the built-in
      mean is framework code, nothing to check) or the check was opted out
      (``HPC_NO_CANARY_REDUCER_CHECK=1``). Byte-identical to a pre-feature run.
    """

    model_config = ConfigDict(extra="forbid", title="canary reducer-check result")

    status: Literal["passed", "disclosed", "unverified", "skipped"] = Field(
        description="passed | disclosed | unverified | skipped — see the class docstring.",
    )
    reducer_cmd: str | None = Field(
        default=None,
        description=(
            "The declared custom aggregate_cmd that was executed against the canary "
            "row. None when skipped (no custom reducer / opted out)."
        ),
    )
    exit_code: int | None = Field(
        default=None,
        description=(
            "The reducer's exit code when the reduce ran to an exit (0 on passed). "
            "None when the reduce could not complete (unverified / skipped) or the "
            "code is not recoverable from the failure surface (disclosed)."
        ),
    )
    stderr_tail: str | None = Field(
        default=None,
        description=(
            "Verbatim reducer error text carried for the disclose / unverified paths "
            "so the human reads the real error. None on a clean pass / skip."
        ),
    )
    output_keys: list[str] | None = Field(
        default=None,
        description=(
            "Sorted top-level keys of the reducer's JSON output on a pass — positive "
            "evidence of the contract SHAPE (never the values). None off the pass path."
        ),
    )
    disclosure: str | None = Field(
        default=None,
        description=(
            "One-line code-rendered disclosure the block loop relays VERBATIM onto the "
            "S2/S3 brief (set on disclosed / unverified). None when there is nothing to "
            "surface (passed / skipped)."
        ),
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
        default_factory=list,
        description=(
            "Main array job ids. EMPTY when the canary failed verification — "
            "the main array never launched (the #160 two-phase gate)."
        ),
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
            "True iff verify-canary returned ok=True — and ONLY then is the "
            "main array launched (#160). False on any canary-side failure (main "
            "never launches; job_ids empty) AND when verification was skipped "
            "(no canary fired / deduped replay)."
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
    canary_skipped_reason: str | None = Field(
        default=None,
        description=(
            "Code-rendered disclosure naming WHY the two-phase GATE skipped the "
            "canary (latency-audit #10 / fallback-inventory S1): the #249 TTL "
            "cache hit — this exact (cmd_sha, version, cluster) was canary-"
            "validated within the 4h window, so the gate honours it instead of "
            "re-running the probe. verified=True with canary_run_id=None then "
            "means 'cached validation stood in', DISTINCT from a canary=false "
            "opt-out (verified=False) and from a failed canary. Null when a canary "
            "actually ran or was opted out. A gated skip is never silent."
        ),
    )
    validated_age_sec: int | None = Field(
        default=None,
        description=(
            "Age in seconds of the cached canary validation the gate honoured "
            "(the structured form of canary_skipped_reason's '<age> ago'). Null "
            "unless canary_skipped_reason is set."
        ),
    )
    reducer_check: ReducerCheckResult | None = Field(
        default=None,
        description=(
            "Rung-2 reducibility check (docs/plans/amortized-reduction-check-2026-07-17.md): "
            "the outcome of EXECUTING the run's declared custom reducer against the verified "
            "canary's ONE real task-0 row, before the main array launches. None when no canary "
            "ran / the run declares no custom reducer / the check was opted out. A `disclosed` "
            "or `unverified` status is a LOUD brief disclosure, never a block — the bare `y` "
            "stands."
        ),
    )
