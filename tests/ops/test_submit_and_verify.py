"""Tests for ``hpc_agent.ops.submit_and_verify``.

The workflow composes two existing workflows (``submit-flow`` and
``verify-canary``); these tests mock both halves at the function level
to assert the composition logic — short-circuit paths and envelope
construction — without driving SSH or the scheduler.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

import pytest

from hpc_agent._wire.workflows.submit_and_verify import SubmitAndVerifySpec
from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _no_double_canary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the double canary out of the pure composition tests.

    These tests assert the two-phase gate's short-circuits + envelope shape; the
    determinism-fingerprint SECOND canary is its own concern (see
    ``test_double_canary.py``). The RANK-8 split fires the second canary
    concurrently (``_fire_second_canary_concurrent``) then verifies it
    (``_verify_second_canary_and_mint``); stub the FIRE to return ``None`` so the
    orchestration takes the single-canary path and ``verify_canary`` /
    ``submit_flow`` call counts stay about the FIRST canary + main array only.
    """
    monkeypatch.setattr(
        "hpc_agent.ops.submit_and_verify._fire_second_canary_concurrent",
        lambda *_a, **_k: None,
    )


def _spec(*, canary: bool = True) -> SubmitAndVerifySpec:
    return SubmitAndVerifySpec(
        submit=SubmitFlowSpec(
            profile="ml",
            cluster="hoffman2",
            ssh_target="user@hoffman2.idre.ucla.edu",
            remote_path="/u/scratch/exp",
            job_name="ml",
            run_id="ml_run_abcd1234",
            total_tasks=4,
            backend="slurm",
            script=".hpc/templates/cpu_array.sh",
            job_env={"K": "v"},
            canary=canary,
        ),
        poll_interval_sec=1,
        wait_budget_sec=5,
    )


def _submit_envelope(*, canary: bool, deduped: bool = False) -> object:
    """Build a frozen-dataclass ``SubmitFlowResult`` matching the live shape.

    The workflow body uses the dataclass form (``hpc_agent.ops.submit_flow``)
    rather than the Pydantic wire form. Two shapes coexist intentionally;
    we mock with the live one.
    """
    from hpc_agent.ops.submit_flow import SubmitFlowResult as _LiveResult

    return _LiveResult(
        run_id="ml_run_abcd1234",
        job_ids=["12345"],
        total_tasks=4,
        deduped=deduped,
        canary_done=canary,
        canary_run_id="ml_run_abcd1234_canary" if canary else None,
        canary_job_ids=["12344"] if canary else None,
    )


def _verify_envelope(*, ok: bool, failure_kind: str | None = None) -> dict:
    return {
        "ok": ok,
        "failure_kind": failure_kind,
        "details": "happy" if ok else "boom",
        "stderr_tail": "" if ok else "RuntimeError\n",
        "metrics_fingerprint": None,
    }


def test_skips_verify_when_canary_disabled(tmp_path: Path) -> None:
    from hpc_agent.ops.submit_and_verify import submit_and_verify

    with (
        mock.patch(
            "hpc_agent.ops.submit_and_verify.submit_flow",
            return_value=_submit_envelope(canary=False),
        ) as m_submit,
        mock.patch("hpc_agent.ops.submit_and_verify.verify_canary") as m_verify,
    ):
        result = submit_and_verify(tmp_path, spec=_spec(canary=False))

    m_submit.assert_called_once()
    m_verify.assert_not_called()
    assert result.verified is False
    assert result.failure_kind is None
    assert result.verify_result is None
    assert result.canary_run_id is None


def _deduped_submit_envelope() -> object:
    """A replay returns deduped=True AND canary_run_id=None — the submit-flow
    convention is "no fresh canary on a replay"."""
    from hpc_agent.ops.submit_flow import SubmitFlowResult as _LiveResult

    return _LiveResult(
        run_id="ml_run_abcd1234",
        job_ids=["12345"],
        total_tasks=4,
        deduped=True,
        canary_done=False,
        canary_run_id=None,
        canary_job_ids=None,
    )


def test_skips_verify_when_submit_dedupes(tmp_path: Path) -> None:
    from hpc_agent.ops.submit_and_verify import submit_and_verify

    with (
        mock.patch(
            "hpc_agent.ops.submit_and_verify.submit_flow",
            return_value=_deduped_submit_envelope(),
        ),
        mock.patch("hpc_agent.ops.submit_and_verify.verify_canary") as m_verify,
    ):
        result = submit_and_verify(tmp_path, spec=_spec(canary=True))

    m_verify.assert_not_called()
    assert result.deduped is True
    assert result.verified is False
    assert result.verify_result is None


def test_passes_through_verify_envelope_on_success(tmp_path: Path) -> None:
    from hpc_agent.ops.submit_and_verify import submit_and_verify

    with (
        mock.patch(
            "hpc_agent.ops.submit_and_verify.submit_flow",
            return_value=_submit_envelope(canary=True),
        ),
        mock.patch(
            "hpc_agent.ops.submit_and_verify.verify_canary",
            return_value=_verify_envelope(ok=True),
        ) as m_verify,
    ):
        result = submit_and_verify(tmp_path, spec=_spec(canary=True))

    m_verify.assert_called_once()
    kw = m_verify.call_args.kwargs
    assert kw["canary_run_id"] == "ml_run_abcd1234_canary"
    assert kw["poll_interval_sec"] == 1
    assert kw["wait_budget_sec"] == 5
    assert result.verified is True
    assert result.failure_kind is None
    assert result.verify_result is not None
    assert result.verify_result.ok is True


def test_enables_checkpoint_verification_when_auto_resume_on(tmp_path: Path) -> None:
    """#294 PR4: when submit.auto_resume_on_kill is set, verify-canary is called
    with verify_checkpoint=True (the canary fired as a checkpoint canary) and the
    explicit checkpoint_result_dir is forwarded."""
    from hpc_agent.ops.submit_and_verify import submit_and_verify

    spec = _spec(canary=True)
    spec = spec.model_copy(
        update={
            "submit": spec.submit.model_copy(update={"auto_resume_on_kill": True}),
            "checkpoint_result_dir": "results/ml_run_abcd1234_canary/task_0",
        }
    )
    with (
        mock.patch(
            "hpc_agent.ops.submit_and_verify.submit_flow",
            return_value=_submit_envelope(canary=True),
        ),
        mock.patch(
            "hpc_agent.ops.submit_and_verify.verify_canary",
            return_value=_verify_envelope(ok=True),
        ) as m_verify,
    ):
        submit_and_verify(tmp_path, spec=spec)

    kw = m_verify.call_args.kwargs
    assert kw["verify_checkpoint"] is True
    assert kw["checkpoint_result_dir"] == "results/ml_run_abcd1234_canary/task_0"


def test_checkpoint_verification_off_by_default(tmp_path: Path) -> None:
    """Without auto_resume_on_kill, verify-canary runs the normal (non-checkpoint)
    gate — verify_checkpoint=False."""
    from hpc_agent.ops.submit_and_verify import submit_and_verify

    with (
        mock.patch(
            "hpc_agent.ops.submit_and_verify.submit_flow",
            return_value=_submit_envelope(canary=True),
        ),
        mock.patch(
            "hpc_agent.ops.submit_and_verify.verify_canary",
            return_value=_verify_envelope(ok=True),
        ) as m_verify,
    ):
        submit_and_verify(tmp_path, spec=_spec(canary=True))

    assert m_verify.call_args.kwargs["verify_checkpoint"] is False


def test_phase2_applies_deterministic_flips(tmp_path: Path) -> None:
    """#279/#185/#283: on a verified canary, the Phase-2 main submit flips canary
    off and requests the rsync+deploy skip via the internal ``_skip_rsync_deploy``
    kwarg (the canary already deployed the same tree) — NOT a spec field (removed
    in #283; the bypass is operator/internal-only now). It also carries NO
    skip_preflight (removed in #275 Fix 2; preflight is operator-gated via
    HPC_AGENT_SKIP_PREFLIGHT, never a spec field)."""
    from hpc_agent.ops.submit_and_verify import submit_and_verify

    with (
        mock.patch(
            "hpc_agent.ops.submit_and_verify.submit_flow",
            return_value=_submit_envelope(canary=True),
        ) as m_submit,
        mock.patch(
            "hpc_agent.ops.submit_and_verify.verify_canary",
            return_value=_verify_envelope(ok=True),
        ),
    ):
        submit_and_verify(tmp_path, spec=_spec(canary=True))

    # Two submit_flow calls: Phase 1 (canary_only) then Phase 2 (main array).
    assert m_submit.call_count == 2
    phase2_call = m_submit.call_args_list[1]
    phase2_spec = phase2_call.kwargs["spec"]
    assert phase2_spec.canary is False
    assert phase2_spec.canary_only is False
    # #283: skip_rsync_deploy is no longer a spec field — the skip is requested
    # via the trusted in-process kwarg instead, so an agent can't assert it.
    assert not hasattr(phase2_spec, "skip_rsync_deploy")
    assert phase2_call.kwargs["_skip_rsync_deploy"] is True
    # skip_preflight is no longer a field on SubmitFlowSpec (#275 Fix 2).
    assert not hasattr(phase2_spec, "skip_preflight")
    assert phase2_call.kwargs["_skip_preflight"] is True


def test_surfaces_failure_kind_from_verify(tmp_path: Path) -> None:
    from hpc_agent.ops.submit_and_verify import submit_and_verify

    with (
        mock.patch(
            "hpc_agent.ops.submit_and_verify.submit_flow",
            return_value=_submit_envelope(canary=True),
        ),
        mock.patch(
            "hpc_agent.ops.submit_and_verify.verify_canary",
            return_value=_verify_envelope(ok=False, failure_kind="import_error"),
        ),
    ):
        result = submit_and_verify(tmp_path, spec=_spec(canary=True))

    assert result.verified is False
    assert result.failure_kind == "import_error"
    assert result.verify_result is not None
    assert "RuntimeError" in result.verify_result.stderr_tail


def test_canary_failure_does_not_launch_main(tmp_path: Path) -> None:
    """#160 gate: a failed canary means the main array is NEVER submitted —
    submit_flow is called once (Phase 1, canary only) and job_ids is empty."""
    from hpc_agent.ops.submit_and_verify import submit_and_verify

    with (
        mock.patch(
            "hpc_agent.ops.submit_and_verify.submit_flow",
            return_value=_submit_envelope(canary=True),
        ) as m_submit,
        mock.patch(
            "hpc_agent.ops.submit_and_verify.verify_canary",
            return_value=_verify_envelope(ok=False, failure_kind="dispatcher_failed"),
        ),
    ):
        result = submit_and_verify(tmp_path, spec=_spec(canary=True))

    assert m_submit.call_count == 1  # Phase 2 (the main array) never ran
    assert result.verified is False
    assert result.failure_kind == "dispatcher_failed"
    assert result.job_ids == []  # main never launched


def test_main_launches_only_after_verified_canary(tmp_path: Path) -> None:
    """#160 gate: Phase 1 submits canary_only; Phase 2 (the main array) runs
    only after the canary verifies, with canary disabled on the second call."""
    from hpc_agent.ops.submit_and_verify import submit_and_verify

    with (
        mock.patch(
            "hpc_agent.ops.submit_and_verify.submit_flow",
            return_value=_submit_envelope(canary=True),
        ) as m_submit,
        mock.patch(
            "hpc_agent.ops.submit_and_verify.verify_canary",
            return_value=_verify_envelope(ok=True),
        ),
    ):
        result = submit_and_verify(tmp_path, spec=_spec(canary=True))

    assert m_submit.call_count == 2
    first = m_submit.call_args_list[0].kwargs["spec"]
    second = m_submit.call_args_list[1].kwargs["spec"]
    assert first.canary is True and first.canary_only is True  # Phase 1: canary only
    assert second.canary is False  # Phase 2: main array, no second canary
    assert result.verified is True
    assert result.job_ids == ["12345"]


def test_composes_metadata_resolves_to_both_workflows() -> None:
    """Mechanism check: the registry's composes graph has both atoms."""
    from hpc_agent._kernel.registry.primitive import (
        get_meta,
        register_primitives,
    )

    register_primitives()
    meta = get_meta("submit-and-verify")
    composed_names = {c.name for c in meta.composes}
    assert composed_names == {"submit-flow", "verify-canary"}


def test_workflow_is_agent_facing_with_cli() -> None:
    from hpc_agent._kernel.registry.primitive import (
        get_meta,
        register_primitives,
    )

    register_primitives()
    meta = get_meta("submit-and-verify")
    assert meta.verb == "workflow"
    assert meta.agent_facing is True
    assert meta.cli is not None
    assert meta.cli.spec_arg is True
    assert meta.cli.spec_model is SubmitAndVerifySpec


def test_idempotency_key_targets_nested_run_id() -> None:
    from hpc_agent._kernel.registry.primitive import (
        get_meta,
        register_primitives,
    )

    register_primitives()
    meta = get_meta("submit-and-verify")
    assert meta.idempotency_key == "submit.run_id"
