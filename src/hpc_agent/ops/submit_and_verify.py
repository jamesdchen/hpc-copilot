"""``submit-and-verify``: workflow composing submit-flow + verify-canary.

One call instead of /submit-hpc then /verify-canary. Submits a run
plus its 1-task canary, then waits for the canary to land terminal
before returning so the caller branches once on ``verified``.

This is a workflow-composes-workflow primitive: ``submit-flow`` and
``verify-canary`` are both workflow-verb primitives in their own
right; ``submit-and-verify`` chains them under one envelope. The
chain is finite and blocking (no monitor-polling shape), so the
composition is honest — the body always calls both halves in order.

Three short-circuit paths:

* ``spec.submit.canary=False`` — the submit half doesn't fire a
  canary, so there's nothing to verify. Return with
  ``verified=False`` and ``verify_result=None``.
* ``submit-flow`` returns ``deduped=True`` (journal already had the
  run) — no fresh canary was submitted; do not pull a stale verify
  off an old canary. Same shape as above.
* The submit half succeeded and a fresh canary is in flight — call
  ``verify-canary`` and pass the envelope through.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.workflows.submit_and_verify import (
    SubmitAndVerifyResult,
    SubmitAndVerifySpec,
)
from hpc_agent._wire.workflows.verify_canary import VerifyCanaryResult
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.ops.submit_flow import submit_flow
from hpc_agent.ops.verify_canary import verify_canary

if TYPE_CHECKING:
    from pathlib import Path


@primitive(
    name="submit-and-verify",
    verb="workflow",
    composes=["submit-flow", "verify-canary"],
    side_effects=[
        SideEffect("scheduler-submit", "<cluster>"),
        SideEffect("ssh", "<cluster> (canary poll + log scan)"),
    ],
    error_codes=[
        errors.SpecInvalid,
        errors.SshUnreachable,
        errors.RemoteCommandFailed,
        errors.ClusterUnknown,
    ],
    idempotent=True,
    idempotency_key="submit.run_id",
    cli=CliShape(
        help=(
            "Submit a run plus its canary, then verify the canary lands "
            "before returning. One call instead of /submit-hpc then "
            "/verify-canary. Returns {run_id, job_ids, deduped, "
            "verified, failure_kind, verify_result}."
        ),
        spec_arg=True,
        spec_model=SubmitAndVerifySpec,
        experiment_dir_arg=True,
        requires_ssh=True,
        schema_ref=SchemaRef(input="submit_and_verify"),
    ),
    agent_facing=True,
)
def submit_and_verify(
    experiment_dir: Path,
    *,
    spec: SubmitAndVerifySpec,
) -> SubmitAndVerifyResult:
    """Run ``submit-flow`` then ``verify-canary`` and return one envelope."""
    submit_result = submit_flow(experiment_dir, spec=spec.submit)

    if not spec.submit.canary or submit_result.canary_run_id is None:
        return SubmitAndVerifyResult(
            run_id=submit_result.run_id,
            job_ids=list(submit_result.job_ids),
            total_tasks=submit_result.total_tasks,
            deduped=submit_result.deduped,
            canary_run_id=submit_result.canary_run_id,
            canary_job_ids=(
                list(submit_result.canary_job_ids) if submit_result.canary_job_ids else None
            ),
            verified=False,
            failure_kind=None,
            verify_result=None,
        )

    verify_envelope = verify_canary(
        experiment_dir,
        canary_run_id=submit_result.canary_run_id,
        expect_output=spec.expect_output,
        fingerprint=spec.fingerprint,
        poll_interval_sec=spec.poll_interval_sec,
        wait_budget_sec=spec.wait_budget_sec,
        log_dir=spec.log_dir,
        file_glob=spec.file_glob,
    )

    verify_result = VerifyCanaryResult.model_validate(verify_envelope)

    return SubmitAndVerifyResult(
        run_id=submit_result.run_id,
        job_ids=list(submit_result.job_ids),
        total_tasks=submit_result.total_tasks,
        deduped=submit_result.deduped,
        canary_run_id=submit_result.canary_run_id,
        canary_job_ids=(
            list(submit_result.canary_job_ids) if submit_result.canary_job_ids else None
        ),
        verified=verify_result.ok,
        failure_kind=verify_result.failure_kind,
        verify_result=verify_result,
    )
