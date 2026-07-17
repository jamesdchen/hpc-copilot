"""The canary reducer check — rung 2 of the reducibility ladder.

``submit_and_verify._check_reducer_on_canary`` EXECUTES a run's declared custom
reducer against the verified canary's ONE real task-0 row before the main array
launches (``docs/plans/amortized-reduction-check-2026-07-17.md``). It runs the
SAME ``cluster_reduce`` the final harvest runs (one-definition), asserts only the
contract SHAPE (never a value), DISCLOSES any reducer error verbatim without ever
refusing the submit, and reports a severed check as ``unverified`` — never
``passed`` (positive-evidence-only).

These tests mock the ``cluster_reduce`` seam exactly as ``test_double_canary.py``
mocks ``submit_flow`` / ``verify_canary`` — nothing touches SSH.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

import pytest

from hpc_agent import errors
from hpc_agent._wire.workflows.submit_and_verify import SubmitAndVerifySpec
from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec

if TYPE_CHECKING:
    from pathlib import Path

_SAV = "hpc_agent.ops.submit_and_verify"
_MAIN = "ml_run_abcd1234"
_CANARY = f"{_MAIN}-canary"
_REDUCER_CMD = "python3 specs/reduce_causal_tune.py"


def _base(run_id: str = _MAIN) -> SubmitFlowSpec:
    return SubmitFlowSpec(
        profile="ml",
        cluster="hoffman2",
        ssh_target="user@hoffman2.idre.ucla.edu",
        remote_path="/u/scratch/exp",
        job_name="ml",
        run_id=run_id,
        total_tasks=4,
        backend="slurm",
        script=".hpc/templates/cpu_array.sh",
        job_env={"K": "v"},
        canary=True,
    )


def _write_main_sidecar(tmp_path: Path, *, run_id: str = _MAIN, reducer: str | None) -> None:
    """Write the MAIN run's sidecar; ``reducer`` declares the custom aggregate_cmd."""
    from hpc_agent.state.runs import write_run_sidecar

    write_run_sidecar(
        tmp_path,
        run_id=run_id,
        cmd_sha="a" * 64,
        hpc_agent_version="0.0.0",
        submitted_at="2026-07-17T00:00:00Z",
        executor="python train.py --seed $SEED",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=4,
        tasks_py_sha="b" * 64,
        cluster="hoffman2",
        remote_path="/u/scratch/exp",
        aggregate_defaults=({"aggregate_cmd": reducer} if reducer is not None else None),
    )


# ── the helper, cluster_reduce mocked ────────────────────────────────────────


def test_passed_on_valid_json_shape(tmp_path: Path) -> None:
    """A reducer that exits 0 and emits parseable JSON → passed, with the output
    top-level keys captured (contract SHAPE) but NO value asserted."""
    from hpc_agent.ops.submit_and_verify import _check_reducer_on_canary

    _write_main_sidecar(tmp_path, reducer=_REDUCER_CMD)
    reduced = {"qlike": 1.234, "n_rows": 1}
    with mock.patch(
        f"{_SAV}.cluster_reduce",
        return_value={"ok": True, "reduced": reduced, "exit_code": 0, "stderr_tail": ""},
    ):
        result = _check_reducer_on_canary(tmp_path, _base(), _CANARY)

    assert result.status == "passed"
    assert result.exit_code == 0
    assert result.reducer_cmd == _REDUCER_CMD
    # Positive evidence of SHAPE — the keys, never the values.
    assert result.output_keys == ["n_rows", "qlike"]
    assert result.disclosure is None


def test_discloses_not_refuses(tmp_path: Path) -> None:
    """A reducer that RAN and FAILED (RemoteCommandFailed: non-zero exit / missing
    output / non-JSON) is a DISCLOSURE carrying the VERBATIM error — never a hard
    refusal. The bare `y` still crosses it."""
    from hpc_agent.ops.submit_and_verify import _check_reducer_on_canary

    _write_main_sidecar(tmp_path, reducer=_REDUCER_CMD)
    verbatim = (
        f"reducer for run_id={_CANARY!r} exited 1: Traceback (most recent call "
        "last): ModuleNotFoundError: No module named 'pandas'"
    )
    with mock.patch(f"{_SAV}.cluster_reduce", side_effect=errors.RemoteCommandFailed(verbatim)):
        result = _check_reducer_on_canary(tmp_path, _base(), _CANARY)

    assert result.status == "disclosed"
    assert result.reducer_cmd == _REDUCER_CMD
    # The stderr is carried VERBATIM (the machinery never paraphrases the error).
    assert verbatim in (result.stderr_tail or "")
    assert result.disclosure and verbatim in result.disclosure
    # Never a pass, never an "unverified" (the reducer produced positive evidence).
    assert result.status != "passed"


def test_needs_two_rows_false_alarm_still_only_discloses(tmp_path: Path) -> None:
    """A reducer that legitimately asserts ≥2 rows (a pairwise DM stat) exits
    non-zero on the single canary row — a real false-alarm source. The machinery
    must render it as a plain DISCLOSURE (verbatim stderr) and NOT interpret
    "needs more rows" vs "broken code" — it surfaces and stops."""
    from hpc_agent.ops.submit_and_verify import _check_reducer_on_canary

    _write_main_sidecar(tmp_path, reducer=_REDUCER_CMD)
    stderr = f"reducer for run_id={_CANARY!r} exited 1: AssertionError: need >= 2 models for DM"
    with mock.patch(f"{_SAV}.cluster_reduce", side_effect=errors.RemoteCommandFailed(stderr)):
        result = _check_reducer_on_canary(tmp_path, _base(), _CANARY)

    assert result.status == "disclosed"  # NOT a refusal, NOT an interpretation
    assert stderr in (result.stderr_tail or "")


@pytest.mark.parametrize(
    "exc",
    [
        errors.SshUnreachable("connection refused"),
        errors.SshCircuitOpen("breaker open for hoffman2"),
        errors.ClusterTimeout("reducer exceeded 300s"),
    ],
)
def test_severed_check_is_unverified_not_pass(tmp_path: Path, exc: Exception) -> None:
    """A check that could not COMPLETE (severed / breaker open / timeout) is
    UNKNOWN → `unverified`, NEVER `passed` (positive-evidence-only)."""
    from hpc_agent.ops.submit_and_verify import _check_reducer_on_canary

    _write_main_sidecar(tmp_path, reducer=_REDUCER_CMD)
    with mock.patch(f"{_SAV}.cluster_reduce", side_effect=exc):
        result = _check_reducer_on_canary(tmp_path, _base(), _CANARY)

    assert result.status == "unverified"
    assert result.status != "passed"
    assert result.disclosure and "UNVERIFIED" in result.disclosure


def test_unexpected_error_is_unverified_never_pass(tmp_path: Path) -> None:
    """Any unexpected raise degrades to `unverified` (best-effort; unknown is
    never a pass) — the check never fails the submit."""
    from hpc_agent.ops.submit_and_verify import _check_reducer_on_canary

    _write_main_sidecar(tmp_path, reducer=_REDUCER_CMD)
    with mock.patch(f"{_SAV}.cluster_reduce", side_effect=RuntimeError("boom")):
        result = _check_reducer_on_canary(tmp_path, _base(), _CANARY)

    assert result.status == "unverified"


def test_uses_cluster_reduce_not_inlined(tmp_path: Path) -> None:
    """One-definition: the check runs the SAME `cluster_reduce` the final harvest
    runs — over the CANARY's run_id with the MAIN run's declared aggregate_cmd —
    never an inlined reduction of its own."""
    from hpc_agent.ops.submit_and_verify import _check_reducer_on_canary

    _write_main_sidecar(tmp_path, reducer=_REDUCER_CMD)
    with mock.patch(
        f"{_SAV}.cluster_reduce",
        return_value={"ok": True, "reduced": {"m": 1}, "exit_code": 0, "stderr_tail": ""},
    ) as m_reduce:
        _check_reducer_on_canary(tmp_path, _base(), _CANARY)

    m_reduce.assert_called_once()
    kwargs = m_reduce.call_args.kwargs
    assert kwargs["run_id"] == _CANARY  # the canary run, not the main
    assert kwargs["aggregate_cmd"] == _REDUCER_CMD  # the main run's declared reducer
    # A SMALL bounded timeout (not the 1800s harvest default) so a hung reducer
    # never stalls the S2→S3 window.
    assert kwargs["timeout_sec"] == 300


def test_no_custom_reducer_skips_byte_identical(tmp_path: Path) -> None:
    """A run declaring NO custom reducer (the built-in mean = framework code)
    SKIPS — cluster_reduce is never called, byte-identical to a pre-feature run."""
    from hpc_agent.ops.submit_and_verify import _check_reducer_on_canary

    _write_main_sidecar(tmp_path, reducer=None)
    with mock.patch(f"{_SAV}.cluster_reduce") as m_reduce:
        result = _check_reducer_on_canary(tmp_path, _base(), _CANARY)

    assert result.status == "skipped"
    m_reduce.assert_not_called()


def test_missing_sidecar_skips(tmp_path: Path) -> None:
    """No main sidecar at all (unreadable gate) degrades to `skipped` — the check
    is purely additive and never runs when it cannot read the declared reducer."""
    from hpc_agent.ops.submit_and_verify import _check_reducer_on_canary

    with mock.patch(f"{_SAV}.cluster_reduce") as m_reduce:
        result = _check_reducer_on_canary(tmp_path, _base(), _CANARY)

    assert result.status == "skipped"
    m_reduce.assert_not_called()


def test_env_opt_out_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """HPC_NO_CANARY_REDUCER_CHECK=1 (operator env, mirroring HPC_NO_DOUBLE_CANARY)
    SKIPS the check even when a custom reducer IS declared."""
    from hpc_agent.ops.submit_and_verify import _check_reducer_on_canary

    _write_main_sidecar(tmp_path, reducer=_REDUCER_CMD)
    monkeypatch.setenv("HPC_NO_CANARY_REDUCER_CHECK", "1")
    with mock.patch(f"{_SAV}.cluster_reduce") as m_reduce:
        result = _check_reducer_on_canary(tmp_path, _base(), _CANARY)

    assert result.status == "skipped"
    m_reduce.assert_not_called()


def test_no_canary_run_id_skips(tmp_path: Path) -> None:
    """No canary run id (no fixture to reduce over) → skipped."""
    from hpc_agent.ops.submit_and_verify import _check_reducer_on_canary

    _write_main_sidecar(tmp_path, reducer=_REDUCER_CMD)
    with mock.patch(f"{_SAV}.cluster_reduce") as m_reduce:
        result = _check_reducer_on_canary(tmp_path, _base(), None)

    assert result.status == "skipped"
    m_reduce.assert_not_called()


# ── the orchestration: submit proceeds through a disclosed check ─────────────


def _submit_env() -> object:
    from hpc_agent.ops.submit_flow import SubmitFlowResult

    return SubmitFlowResult(
        run_id=_MAIN,
        job_ids=["12345"],
        total_tasks=4,
        deduped=False,
        canary_done=True,
        canary_run_id=_CANARY,
        canary_job_ids=["12344"],
    )


def _verify_ok() -> dict:
    return {
        "ok": True,
        "failure_kind": None,
        "details": "happy",
        "stderr_tail": "",
        "metrics_fingerprint": None,
    }


def test_submit_proceeds_when_reducer_check_discloses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: a reducer that FAILS on the canary row does NOT block the
    submit — the main array still launches and the disclosed check rides the
    result (disclose-never-block; the bare `y` stands)."""
    from hpc_agent.ops.submit_and_verify import submit_and_verify

    monkeypatch.setenv("HPC_NO_DOUBLE_CANARY", "1")  # focus on the single-canary gate
    _write_main_sidecar(tmp_path, reducer=_REDUCER_CMD)
    spec = SubmitAndVerifySpec(submit=_base(), poll_interval_sec=1, wait_budget_sec=5)

    with (
        mock.patch(f"{_SAV}.submit_flow", return_value=_submit_env()) as m_submit,
        mock.patch(f"{_SAV}.verify_canary", return_value=_verify_ok()),
        mock.patch(
            f"{_SAV}.cluster_reduce",
            side_effect=errors.RemoteCommandFailed("reducer exited 1: SyntaxError"),
        ),
    ):
        result = submit_and_verify(tmp_path, spec=spec)

    # The submit was NEVER refused — the main array launched.
    assert result.verified is True
    assert result.job_ids == ["12345"]
    assert m_submit.call_count == 2  # phase-1 canary + phase-2 main
    # The disclosed check rides the result verbatim.
    assert result.reducer_check is not None
    assert result.reducer_check.status == "disclosed"
    assert "SyntaxError" in (result.reducer_check.stderr_tail or "")


def test_stop_after_canary_carries_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At the S2 boundary (stop_after_canary), a passed check rides the returned
    result so submit-s2 can attach it to the brief."""
    from hpc_agent.ops.submit_and_verify import submit_and_verify

    monkeypatch.setenv("HPC_NO_DOUBLE_CANARY", "1")
    _write_main_sidecar(tmp_path, reducer=_REDUCER_CMD)
    spec = SubmitAndVerifySpec(submit=_base(), poll_interval_sec=1, wait_budget_sec=5)

    with (
        mock.patch(f"{_SAV}.submit_flow", return_value=_submit_env()),
        mock.patch(f"{_SAV}.verify_canary", return_value=_verify_ok()),
        mock.patch(
            f"{_SAV}.cluster_reduce",
            return_value={"ok": True, "reduced": {"qlike": 1.0}, "exit_code": 0, "stderr_tail": ""},
        ),
    ):
        result = submit_and_verify(tmp_path, spec=spec, stop_after_canary=True)

    assert result.verified is True
    assert result.job_ids == []  # main did NOT launch at S2
    assert result.reducer_check is not None
    assert result.reducer_check.status == "passed"
