"""The gated submit-s2 path honours the #249 canary TTL cache (latency-audit #10).

submit-flow's own #249 arm never fired under the two-phase gate: Phase 1 forces
``canary_only=True``, which ``_canary_decision`` reads as "always canary." These
tests pin the gate's OWN cache-skip decision (``_gated_canary_cache_decision``),
its read-only ssh-breaker EVENT invalidation, and the end-to-end
``submit_and_verify`` short-circuit — the disclosure line, the structured age,
the distinct-from-opt-out result shape, and the HPC_NO_CANARY_SKIP force knob.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

from hpc_agent._wire.workflows.submit_and_verify import SubmitAndVerifySpec
from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec
from hpc_agent.ops import submit_and_verify as sav
from hpc_agent.state import canary_cache

if TYPE_CHECKING:
    from pathlib import Path

SSH_TARGET = "user@hoffman2.idre.ucla.edu"
HOST = "hoffman2.idre.ucla.edu"
CMD_SHA = "abcdef0123456789"
CLUSTER = "hoffman2"


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the canary cache AND the ssh-breaker state under a temp journal."""
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path))
    for var in ("HPC_NO_CANARY_SKIP", "HPC_CANARY_TTL_SEC", "HPC_AGENT_ALWAYS_CANARY"):
        monkeypatch.delenv(var, raising=False)


def _base(**over: Any) -> SubmitFlowSpec:
    fields: dict[str, Any] = dict(
        profile="ml",
        cluster=CLUSTER,
        ssh_target=SSH_TARGET,
        remote_path="/u/scratch/exp",
        job_name="ml",
        run_id="ml_run_abcd1234",
        total_tasks=100,
        backend="slurm",
        script=".hpc/templates/cpu_array.sh",
        job_env={"EXECUTOR": "python3 .hpc/_hpc_dispatch.py", "HPC_CMD_SHA": CMD_SHA},
        canary=True,
    )
    fields.update(over)
    return SubmitFlowSpec(**fields)


def _record_fresh_validation() -> None:
    from hpc_agent import __version__ as ver

    key = canary_cache.canary_cache_key(cmd_sha=CMD_SHA, version=ver or "", cluster=CLUSTER)
    canary_cache.record_canary_validated(key)


def _open_breaker_at(epoch: float) -> None:
    """Open the host's circuit breaker as of wall-clock *epoch* (3 failures)."""
    from hpc_agent.infra import ssh_circuit

    for _ in range(ssh_circuit.CIRCUIT_THRESHOLD):
        ssh_circuit.record_connection_failure(
            SSH_TARGET, detail="connection timed out", clock=lambda: epoch
        )


# ── the gate's own cache decision ────────────────────────────────────────────


def test_fresh_cache_hit_skips_the_gated_canary() -> None:
    _record_fresh_validation()
    decision = sav._gated_canary_cache_decision(_base())
    assert decision is not None
    assert decision.skip is True
    assert decision.validated_age_sec is not None and decision.validated_age_sec >= 0
    # The mandatory disclosure line (fallback-inventory S1 shape).
    assert decision.reason is not None
    assert "canary skipped" in decision.reason
    assert CMD_SHA[:8] in decision.reason
    assert CLUSTER in decision.reason
    assert "HPC_NO_CANARY_SKIP=1 to force" in decision.reason


def test_stale_absent_cache_runs_the_canary() -> None:
    # No validation recorded → no hit → run the ordinary canary (decision None).
    assert sav._gated_canary_cache_decision(_base()) is None


def test_no_cmd_sha_runs_the_canary() -> None:
    _record_fresh_validation()
    # A spec with no HPC_CMD_SHA cannot key the cache → run the canary.
    assert sav._gated_canary_cache_decision(_base(job_env={"EXECUTOR": "x"})) is None


def test_force_canary_ignores_the_cache() -> None:
    _record_fresh_validation()
    assert sav._gated_canary_cache_decision(_base(force_canary=True)) is None


def test_no_canary_skip_env_forces_the_canary(monkeypatch: pytest.MonkeyPatch) -> None:
    _record_fresh_validation()
    monkeypatch.setenv("HPC_NO_CANARY_SKIP", "1")
    # The SAME knob the ungated arm uses — reused, not re-minted.
    assert sav._gated_canary_cache_decision(_base()) is None


def test_always_canary_env_forces_the_canary(monkeypatch: pytest.MonkeyPatch) -> None:
    _record_fresh_validation()
    monkeypatch.setenv("HPC_AGENT_ALWAYS_CANARY", "1")
    assert sav._gated_canary_cache_decision(_base()) is None


# ── read-only ssh-breaker EVENT invalidation ─────────────────────────────────


def test_breaker_incident_after_validation_ignores_the_hit() -> None:
    import time

    _record_fresh_validation()
    # A breaker OPEN recorded ~12 min AFTER the validation invalidates the boot
    # proof even though the entry is still inside the 4h TTL.
    _open_breaker_at(time.time() + 720.0)
    decision = sav._gated_canary_cache_decision(_base())
    assert decision is not None
    assert decision.skip is False
    assert decision.reason is not None
    assert "canary cache hit ignored" in decision.reason
    assert "breaker opened" in decision.reason
    assert HOST in decision.reason


def test_breaker_incident_before_validation_still_skips() -> None:
    import time

    # Breaker opened well BEFORE the validation → not a post-validation event →
    # the fresh hit still skips (the incident predates the boot proof).
    _open_breaker_at(time.time() - 3600.0)
    _record_fresh_validation()
    decision = sav._gated_canary_cache_decision(_base())
    assert decision is not None
    assert decision.skip is True


def test_absent_breaker_state_skips_fail_open() -> None:
    # No breaker state file at all → fail-open by breaker doctrine → honour cache.
    _record_fresh_validation()
    decision = sav._gated_canary_cache_decision(_base())
    assert decision is not None and decision.skip is True


# ── end-to-end submit_and_verify short-circuit ───────────────────────────────


def _verify_spec() -> SubmitAndVerifySpec:
    return SubmitAndVerifySpec(submit=_base(), poll_interval_sec=1, wait_budget_sec=5)


def _staged_result() -> object:
    from hpc_agent.ops.submit_flow import SubmitFlowResult

    return SubmitFlowResult(
        run_id="ml_run_abcd1234",
        job_ids=[],
        total_tasks=100,
        deduped=False,
        canary_done=False,
        main_launched=False,
    )


def _main_result() -> object:
    from hpc_agent.ops.submit_flow import SubmitFlowResult

    return SubmitFlowResult(
        run_id="ml_run_abcd1234",
        job_ids=["99999"],
        total_tasks=100,
        deduped=False,
        canary_done=False,
        main_launched=True,
    )


def test_gated_skip_stop_after_canary_stages_and_discloses(tmp_path: Path) -> None:
    _record_fresh_validation()
    with (
        mock.patch(
            "hpc_agent.ops.submit_and_verify.submit_flow", return_value=_staged_result()
        ) as m_submit,
        mock.patch("hpc_agent.ops.submit_and_verify.verify_canary") as m_verify,
    ):
        result = sav.submit_and_verify(tmp_path, spec=_verify_spec(), stop_after_canary=True)

    # No canary was fired/verified — the cached validation stood in.
    m_verify.assert_not_called()
    # The tree was STILL staged (prelude runs), via the canary_only override skip.
    m_submit.assert_called_once()
    _, kwargs = m_submit.call_args
    assert kwargs["_canary_decision_override"][0] is False
    # verified=True but canary_run_id=None → DISTINCT from a canary=false opt-out.
    assert result.verified is True
    assert result.canary_run_id is None
    assert result.job_ids == []
    assert result.canary_skipped_reason is not None
    assert "canary skipped" in result.canary_skipped_reason
    assert result.validated_age_sec is not None


def test_gated_skip_fused_launches_main_without_canary(tmp_path: Path) -> None:
    _record_fresh_validation()
    with (
        mock.patch(
            "hpc_agent.ops.submit_and_verify.submit_flow", return_value=_main_result()
        ) as m_submit,
        mock.patch("hpc_agent.ops.submit_and_verify.verify_canary") as m_verify,
    ):
        result = sav.submit_and_verify(tmp_path, spec=_verify_spec(), stop_after_canary=False)

    m_verify.assert_not_called()
    # Fused path: canary=false stages AND launches the main array in one call.
    _, kwargs = m_submit.call_args
    assert kwargs.get("_canary_decision_override") is None
    assert result.verified is True
    assert result.canary_run_id is None
    assert result.job_ids == ["99999"]
    assert result.canary_skipped_reason is not None


def test_breaker_incident_runs_the_real_canary_with_why_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    _record_fresh_validation()
    _open_breaker_at(time.time() + 720.0)
    # Single-canary path: stub the concurrent second canary out.
    monkeypatch.setattr(sav, "_fire_second_canary_concurrent", lambda *_a, **_k: None)

    from hpc_agent.ops.submit_flow import SubmitFlowResult

    canary_env = SubmitFlowResult(
        run_id="ml_run_abcd1234",
        job_ids=[],
        total_tasks=100,
        deduped=False,
        canary_done=True,
        canary_run_id="ml_run_abcd1234-canary",
        canary_job_ids=["12344"],
        main_launched=False,
    )
    verify_ok = {
        "ok": True,
        "failure_kind": None,
        "details": "ok",
        "stderr_tail": "",
        "metrics_fingerprint": None,
    }
    with (
        mock.patch("hpc_agent.ops.submit_and_verify.submit_flow", return_value=canary_env),
        mock.patch(
            "hpc_agent.ops.submit_and_verify.verify_canary", return_value=verify_ok
        ) as m_verify,
        mock.patch("hpc_agent.ops.submit_and_verify._mark_canary_terminal"),
    ):
        result = sav.submit_and_verify(tmp_path, spec=_verify_spec(), stop_after_canary=True)

    # The cache hit was IGNORED — a real canary ran and verified.
    m_verify.assert_called_once()
    assert result.verified is True
    assert result.canary_run_id == "ml_run_abcd1234-canary"
    # No skip disclosure (the canary actually ran)…
    assert result.canary_skipped_reason is None
    # …but the ignored-why-line was disclosed to the operator.
    assert "canary cache hit ignored" in capsys.readouterr().err
