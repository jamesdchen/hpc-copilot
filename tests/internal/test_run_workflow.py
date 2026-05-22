"""run_workflow — the code-orchestrated workflow entrypoint."""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent._internal.invoke import InvocationResult, RenderedPrompt
from hpc_agent._internal.run_workflow import run_workflow
from hpc_agent.atoms.spawn_prompt import SpawnContractError


class _StubInvoker:
    """A WorkerInvoker that returns canned output without spawning anything."""

    name = "stub"

    def __init__(self, output: str, exit_code: int = 0) -> None:
        self._output = output
        self._exit_code = exit_code

    def invoke(self, prompt: RenderedPrompt, *, cwd: Path) -> InvocationResult:
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
    # A worker that produces no valid report is an internal failure
    # (HpcError) — not a SpawnContractError (which means a bad request).
    _use(monkeypatch, _StubInvoker("boom — no json here", exit_code=3))
    with pytest.raises(errors.HpcError, match="did not return a valid report"):
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


def test_cmd_run_success_envelope(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # cmd_run's success path: a returned WorkerReport is shaped into the
    # {report, worker_exit_code} envelope. Stubs run_workflow so no
    # worker is spawned.
    import argparse
    import json

    from hpc_agent import agent_cli
    from hpc_agent._schema_models.spawn_contract import WorkerReport

    report = WorkerReport(result={"run_id": "r1"}, anomalies="")
    monkeypatch.setattr(
        "hpc_agent._internal.run_workflow.run_workflow",
        lambda **kwargs: (report, 0),
    )
    rc = agent_cli.cmd_run(
        argparse.Namespace(workflow="submit", experiment_dir=Path("."), fields_json="{}")
    )
    assert rc == 0
    env = json.loads(capsys.readouterr().out.strip())
    assert env["ok"] is True
    assert env["data"]["report"]["result"] == {"run_id": "r1"}
    assert env["data"]["worker_exit_code"] == 0
