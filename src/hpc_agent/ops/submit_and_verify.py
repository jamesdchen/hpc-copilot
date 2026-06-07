"""``submit-and-verify``: two-phase canary gate over submit-flow + verify-canary.

One call instead of /submit-hpc then /verify-canary. The canary is a GATE
(#160): submit the 1-task canary FIRST (``canary_only``), verify it lands and
produces output, and launch the main array ONLY on success — so a broken
dispatch never reaches the full run.

This is a workflow-composes-workflow primitive: ``submit-flow`` and
``verify-canary`` are both workflow-verb primitives in their own right;
``submit-and-verify`` chains them under one envelope.

Paths:

* ``spec.submit.canary=False`` — no canary, so the main array submits directly
  and there's nothing to verify. ``verified=False``, ``verify_result=None``.
* Phase 1 ``submit-flow`` returns ``deduped=True`` (the run already exists) —
  no fresh canary; pass the submit result through without a stale verify.
* Canary verified → Phase 2 launches the main array; ``verified=True`` with the
  main ``job_ids``.
* Canary FAILED → the main array never launches; ``verified=False``,
  ``failure_kind`` set, and ``job_ids`` empty.
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
    """Two-phase canary gate (#160): submit the canary, verify it, then launch
    the main array ONLY on a verified canary — never before.

    Phase 1 submits the canary alone (``canary_only=True``); on a verified
    canary, Phase 2 submits the main array (``canary=False``). A failed canary
    returns ``verified=False`` with empty ``job_ids`` — the main NEVER launches.
    """
    base = spec.submit

    # No canary requested → submit the main array directly; nothing to gate.
    if not base.canary:
        result = submit_flow(experiment_dir, spec=base)
        return SubmitAndVerifyResult(
            run_id=result.run_id,
            job_ids=list(result.job_ids),
            total_tasks=result.total_tasks,
            deduped=result.deduped,
            canary_run_id=result.canary_run_id,
            canary_job_ids=(list(result.canary_job_ids) if result.canary_job_ids else None),
            verified=False,
            failure_kind=None,
            verify_result=None,
        )

    # Phase 1 — submit ONLY the canary; the main array does NOT launch yet.
    canary_submit = submit_flow(
        experiment_dir, spec=base.model_copy(update={"canary": True, "canary_only": True})
    )

    # Deduped (the run already exists) or no canary landed → don't gate; pass
    # the submit result through without pulling a stale verify.
    if canary_submit.deduped or canary_submit.canary_run_id is None:
        return SubmitAndVerifyResult(
            run_id=canary_submit.run_id,
            job_ids=list(canary_submit.job_ids),
            total_tasks=canary_submit.total_tasks,
            deduped=canary_submit.deduped,
            canary_run_id=canary_submit.canary_run_id,
            canary_job_ids=(
                list(canary_submit.canary_job_ids) if canary_submit.canary_job_ids else None
            ),
            verified=False,
            failure_kind=None,
            verify_result=None,
        )

    # Verify the canary — THE GATE. #294 PR4: an auto_resume_on_kill run fired a
    # CHECKPOINT canary (HPC_CHECKPOINT_CANARY=1), so verification swaps to the
    # round-trip assertion (a loadable checkpoint survived the kill) instead of
    # the exit-0/output criteria — a preempted canary is the expected outcome.
    verify_result = VerifyCanaryResult.model_validate(
        verify_canary(
            experiment_dir,
            canary_run_id=canary_submit.canary_run_id,
            expect_output=spec.expect_output,
            fingerprint=spec.fingerprint,
            verify_checkpoint=base.auto_resume_on_kill,
            checkpoint_result_dir=spec.checkpoint_result_dir,
            poll_interval_sec=spec.poll_interval_sec,
            wait_budget_sec=spec.wait_budget_sec,
            log_dir=spec.log_dir,
            file_glob=spec.file_glob,
        )
    )
    canary_job_ids = list(canary_submit.canary_job_ids) if canary_submit.canary_job_ids else None

    if not verify_result.ok:
        # Canary failed → refuse to launch the main array (#160). job_ids is
        # empty: the main never went out.
        return SubmitAndVerifyResult(
            run_id=canary_submit.run_id,
            job_ids=[],
            total_tasks=canary_submit.total_tasks,
            deduped=False,
            canary_run_id=canary_submit.canary_run_id,
            canary_job_ids=canary_job_ids,
            verified=False,
            failure_kind=verify_result.failure_kind,
            verify_result=verify_result,
        )

    # Phase 2 — canary verified → launch the main array. The deterministic
    # Phase-2 flips (#279, mirrored by the prepare-phase2-spec primitive): no
    # canary, launch main, and skip the rsync+deploy Phase 1 already did (#185).
    # No ``skip_preflight`` here — preflight is operator-gated now (#275 Fix 2);
    # Phase 1's probe plus the #255 TTL cache already cover the re-check cheaply.
    main_submit = submit_flow(
        experiment_dir,
        # #185: Phase 1 just deployed, so the main launch skips the redundant
        # rsync+deploy via ``skip_rsync_deploy``.
        spec=base.model_copy(
            update={"canary": False, "canary_only": False, "skip_rsync_deploy": True}
        ),
        # #275: skip_preflight is no longer a spec field. Phase 1 (the canary
        # submit) already paid the preflight, so the main-array launch skips the
        # redundant probe via the internal operator-trusted kwarg — not an
        # agent-visible spec field an agent could set to silence the runtime probe.
        _skip_preflight=True,
    )
    return SubmitAndVerifyResult(
        run_id=main_submit.run_id,
        job_ids=list(main_submit.job_ids),
        total_tasks=main_submit.total_tasks,
        deduped=main_submit.deduped,
        canary_run_id=canary_submit.canary_run_id,
        canary_job_ids=canary_job_ids,
        verified=True,
        failure_kind=None,
        verify_result=verify_result,
    )
