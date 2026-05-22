"""run_workflow — the code-orchestrated workflow entrypoint."""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_agent._internal.invoke import InvocationResult
from hpc_agent._internal.run_workflow import run_workflow
from hpc_agent.atoms.spawn_prompt import SpawnContractError


class _StubInvoker:
    """A WorkerInvoker that returns canned output without spawning anything."""

    name = "stub"

    def __init__(self, output: str, exit_code: int = 0) -> None:
        self._output = output
        self._exit_code = exit_code

    def invoke(self, prompt: str, *, cwd: Path) -> InvocationResult:
        return InvocationResult(exit_code=self._exit_code, output=self._output)


def _use(monkeypatch: pytest.MonkeyPatch, stub: _StubInvoker) -> None:
    monkeypatch.setattr("hpc_agent._internal.run_workflow.get_invoker", lambda name=None: stub)


def test_run_workflow_parses_a_valid_report(monkeypatch: pytest.MonkeyPatch) -> None:
    out = (
        '{"result": {"run_id": "r1"}, "decisions": '
        '[{"point": "canary", "outcome": "passed", "why": "1/1 ok"}], '
        '"anomalies": ""}'
    )
    _use(monkeypatch, _StubInvoker(out))
    report, code = run_workflow(workflow="submit", experiment_dir=".", fields={"cluster": "sge1"})
    assert code == 0
    assert report.result == {"run_id": "r1"}
    assert report.decisions[0].point == "canary"


def test_run_workflow_rejects_an_invalid_request() -> None:
    # validate_and_render raises before any worker is invoked.
    with pytest.raises(SpawnContractError):
        run_workflow(workflow="nope", experiment_dir=".", fields={})


def test_run_workflow_surfaces_a_worker_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(monkeypatch, _StubInvoker("boom — no json here", exit_code=3))
    with pytest.raises(SpawnContractError, match="exited 3"):
        run_workflow(workflow="submit", experiment_dir=".", fields={})


def test_cli_run_rejects_bad_fields_json() -> None:
    # The bad-JSON path fails before any worker invocation, so this
    # exercises the real CLI without spawning claude.
    from tests.cli._helpers import parse_envelope, run_cli

    rc, out, _ = run_cli("run", "--workflow", "submit", "--fields-json", "not-json")
    assert rc == 1
    env = parse_envelope(out)
    assert env["ok"] is False
    assert env["error_code"] == "spec_invalid"
