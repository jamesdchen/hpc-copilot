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
from hpc_agent.ops.submit_flow import SubmitFlowResult, submit_flow
from hpc_agent.ops.verify_canary import verify_canary

if TYPE_CHECKING:
    from pathlib import Path

    from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec

__all__ = ["launch_main_array", "submit_and_verify"]


def _mark_canary_terminal(experiment_dir: Path, canary_run_id: str | None, *, status: str) -> None:
    """Close the canary's RunRecord once its verdict is known (§5 watchdog).

    A canary is a 1-task run with its own ``<main>-canary`` RunRecord, created
    ``in_flight`` at submission. ``verify-canary`` is a side-effect-free query:
    it polls the canary to terminal ON THE CLUSTER but never closes the LOCAL
    record. Left alone, a verified canary lingers ``in_flight`` and the §5
    watchdog / ``doctor`` false-flags it as a stalled driver (and would draft a
    spurious re-arm). Transition it here, where the canary's lifecycle is owned
    (``submit_and_verify`` submitted it). Best-effort: a bookkeeping stamp must
    never fail the submit gate — a deduped/cache-hit canary has no fresh record
    (``FileNotFoundError`` is benign), and any other stamp error is warned, not
    raised (the next reconcile re-derives ground truth regardless).
    """
    if not canary_run_id:
        return
    try:
        from hpc_agent.state.journal import mark_run

        mark_run(experiment_dir, canary_run_id, status=status)
    except FileNotFoundError:
        pass  # deduped / cache-hit canary — no fresh record to close
    except Exception:  # noqa: BLE001 — a terminal stamp must never fail the gate
        import logging

        logging.getLogger(__name__).warning(
            "failed to mark canary %r terminal (status=%s); doctor may "
            "transiently flag it as stalled until the next reconcile",
            canary_run_id,
            status,
            exc_info=True,
        )


def _launch_main_array(experiment_dir: Path, base: SubmitFlowSpec) -> SubmitFlowResult:
    """Phase-2 of the two-phase gate: launch the main array after a verified canary.

    Extracted so both the fused path (``submit_and_verify`` continuing past the
    canary) and the block-split S3 path (:func:`launch_main_array`) issue the
    IDENTICAL deterministic Phase-2 submit-flow call: canary off, and skip the
    rsync+deploy+preflight Phase 1 already paid (#185/#275/#283). Those skips
    ride internal operator-trusted kwargs — "Phase 1 just deployed this tree" is
    a structural fact the code knows here — never agent-visible spec fields.
    """
    return submit_flow(
        experiment_dir,
        spec=base.model_copy(update={"canary": False, "canary_only": False}),
        _skip_preflight=True,
        _skip_rsync_deploy=True,
    )


def _assert_no_post_greenlight_drift(experiment_dir: Path, base: SubmitFlowSpec) -> None:
    """Refuse the S3 main-array launch if the tree drifted since the S2 greenlight.

    The S2→S3 seam has a human review gap: S2 verified a canary against the tree
    as it stood then and recorded that tree's identity onto the run's durable
    per-experiment SIDECAR (``.hpc/runs/<run_id>.json`` — ``tasks_py_sha`` /
    ``executor``). S3 skips rsync+deploy (Phase 1 already shipped that tree), so a
    local edit to ``.hpc/tasks.py`` AFTER the greenlight would silently launch the
    full array on code the canary NEVER verified. This guard closes that gap.

    The journal holds NO main-run record at S3 (the record is minted only when
    ``submit_and_record`` runs, inside the launch we are about to gate), so the
    canary-time baseline is read from the sidecar. Current values are freshly
    derived, mirroring the layer-1 dedup gate (``ops/submit/runner.py``): the
    ``tasks.py`` drift sha is recomputed from ``<experiment_dir>/.hpc/tasks.py``,
    and the current executor is the sidecar's own recorded command (the one S3
    will dispatch — the sidecar is not rewritten between S2 and S3). The
    comparison routes through the single drift predicate
    (:func:`hpc_agent.state.code_drift.detect_code_drift`) — never an inline sha
    compare — whose symmetric rule disables any dimension whose baseline is
    absent (absence ≠ drift), so a missing sidecar / tasks.py just launches.
    """
    import json

    from hpc_agent.state.code_drift import detect_code_drift
    from hpc_agent.state.run_sha import compute_tasks_py_sha
    from hpc_agent.state.runs import read_run_sidecar

    run_id = base.run_id
    try:
        sidecar = read_run_sidecar(experiment_dir, run_id)
    except (FileNotFoundError, OSError, json.JSONDecodeError, UnicodeDecodeError, errors.HpcError):
        # No readable canary-time baseline → cannot prove drift → launch.
        return

    recorded_executor = str(sidecar.get("executor") or "") or None
    recorded_tasks_py_sha = str(sidecar.get("tasks_py_sha") or "") or None

    # Current executor: the sidecar-recorded command S3 dispatches (mirrors
    # layer-1's self-sidecar read). Unchanged between S2 and S3, so this
    # dimension is disabled by the symmetric rule unless the sidecar itself
    # changed — the fireable dimension here is the tasks.py sha below.
    current_executor = recorded_executor
    tasks_py = experiment_dir / ".hpc" / "tasks.py"
    current_tasks_py_sha: str | None = None
    if tasks_py.is_file():
        try:
            current_tasks_py_sha = compute_tasks_py_sha(tasks_py)
        except OSError:
            current_tasks_py_sha = None

    drift = detect_code_drift(
        recorded_executor=recorded_executor,
        recorded_tasks_py_sha=recorded_tasks_py_sha,
        current_executor=current_executor,
        current_tasks_py_sha=current_tasks_py_sha,
    )
    if drift.drifted:
        raise errors.SpecInvalid(
            "tasks.py/executor drifted since the canary greenlight — re-run "
            "submit-s2 so the canary verifies the current tree "
            f"(run_id={run_id!r})."
        )


def launch_main_array(
    experiment_dir: Path,
    *,
    spec: SubmitAndVerifySpec,
    canary_run_id: str | None = None,
    canary_job_ids: list[str] | None = None,
) -> SubmitAndVerifyResult:
    """Launch the main array after a canary was ALREADY verified — the S3 seam.

    The two-phase gate, split across the human boundary (docs/design/
    human-amplification-blocks.md §3): S2 ran ``submit_and_verify(...,
    stop_after_canary=True)`` and handed the human "canary green, est N
    core-hours"; on greenlight, S3 calls this to launch the main array. This
    path does NOT re-verify — the caller asserts the canary passed (that is what
    the human greenlit), so ``verified`` is True. ``canary_run_id`` /
    ``canary_job_ids`` from S2 are threaded onto the result for provenance.

    Before launching, a loud drift guard (:func:`_assert_no_post_greenlight_drift`)
    refuses the launch if ``tasks.py`` drifted since the greenlight — S3 skips
    rsync+deploy, so a post-greenlight local edit would otherwise run the full
    array on code the canary never verified.
    """
    _assert_no_post_greenlight_drift(experiment_dir, spec.submit)
    main_submit = _launch_main_array(experiment_dir, spec.submit)
    return SubmitAndVerifyResult(
        run_id=main_submit.run_id,
        job_ids=list(main_submit.job_ids),
        total_tasks=main_submit.total_tasks,
        deduped=main_submit.deduped,
        canary_run_id=canary_run_id,
        canary_job_ids=(list(canary_job_ids) if canary_job_ids else None),
        verified=True,
        failure_kind=None,
        verify_result=None,
    )


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
    stop_after_canary: bool = False,
) -> SubmitAndVerifyResult:
    """Two-phase canary gate (#160): submit the canary, verify it, then launch
    the main array ONLY on a verified canary — never before.

    Phase 1 submits the canary alone (``canary_only=True``); on a verified
    canary, Phase 2 submits the main array (``canary=False``). A failed canary
    returns ``verified=False`` with empty ``job_ids`` — the main NEVER launches.

    ``stop_after_canary`` inserts the human boundary of the block decomposition
    (docs/design/human-amplification-blocks.md §3): when True, a VERIFIED canary
    returns immediately with ``verified=True`` and EMPTY ``job_ids`` — the main
    array is NOT launched. The human reviews "canary green, est N core-hours"
    (submit-s2) and, on greenlight, S3 launches the main array via
    :func:`launch_main_array`. The default (False) preserves the fused behavior
    for every existing caller: Phase 1 flows straight into Phase 2 in one call.
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
        # empty: the main never went out. Close the canary record as failed so
        # it doesn't linger in_flight and false-flag as a stalled driver (§5).
        _mark_canary_terminal(experiment_dir, canary_submit.canary_run_id, status="failed")
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

    # Canary verified → its 1-task job is terminal on the cluster. Close the
    # canary's RunRecord so the §5 watchdog / doctor stop scanning it as a live
    # in_flight run (both the stop_after_canary S2 return and the Phase-2 launch
    # below are reached only past this point, so one call covers both).
    _mark_canary_terminal(experiment_dir, canary_submit.canary_run_id, status="complete")

    # Canary verified. The block boundary (submit-s2): STOP before the main
    # array so the human can review "canary green, est N core-hours" and
    # greenlight (§3). ``verified=True`` but ``job_ids`` is empty — the main did
    # NOT launch; S3 launches it post-greenlight via ``launch_main_array``.
    if stop_after_canary:
        return SubmitAndVerifyResult(
            run_id=canary_submit.run_id,
            job_ids=[],
            total_tasks=canary_submit.total_tasks,
            deduped=False,
            canary_run_id=canary_submit.canary_run_id,
            canary_job_ids=canary_job_ids,
            verified=True,
            failure_kind=None,
            verify_result=verify_result,
        )

    # Phase 2 — canary verified → launch the main array. The deterministic
    # Phase-2 flips (#279, mirrored by the prepare-phase2-spec primitive): no
    # canary, launch main, and skip the rsync+deploy Phase 1 already did (#185).
    # No ``skip_preflight`` as a spec field — preflight is operator-gated now
    # (#275 Fix 2); Phase 1's probe plus the #255 TTL cache cover the re-check.
    # The same deterministic call backs the block-split S3 path, so both share
    # ``_launch_main_array``.
    main_submit = _launch_main_array(experiment_dir, base)
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
