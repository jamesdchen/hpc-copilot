"""Tests for ``hpc_agent.ops.submit_and_verify``.

The workflow composes two existing workflows (``submit-flow`` and
``verify-canary``); these tests mock both halves at the function level
to assert the composition logic — short-circuit paths and envelope
construction — without driving SSH or the scheduler.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

from hpc_agent._wire.workflows.submit_and_verify import SubmitAndVerifySpec
from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec

if TYPE_CHECKING:
    from pathlib import Path


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
