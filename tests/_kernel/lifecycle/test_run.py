"""run_workflow — the code-orchestrated workflow entrypoint."""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent._kernel.extension.spawn_prompt import SpawnContractError
from hpc_agent._kernel.lifecycle.invoke import InvocationResult, RenderedPrompt
from hpc_agent._kernel.lifecycle.run import run_workflow


class _StubInvoker:
    """A WorkerInvoker that returns canned output without spawning anything."""

    name = "stub"

    def __init__(self, output: str, exit_code: int = 0, *, remediation: str | None = None) -> None:
        self._output = output
        self._exit_code = exit_code
        self._remediation = remediation

    def invoke(self, prompt: RenderedPrompt, *, cwd: Path) -> InvocationResult:
        return InvocationResult(exit_code=self._exit_code, output=self._output)

    def missing_credential_remediation(self) -> str | None:
        return self._remediation


def _use(monkeypatch: pytest.MonkeyPatch, stub: _StubInvoker) -> None:
    monkeypatch.setattr("hpc_agent._kernel.lifecycle.run.get_invoker", lambda name=None: stub)


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


def test_run_workflow_blocks_when_worker_has_no_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A worker with no usable credential (e.g. an OAuth-only parent session)
    # fails fast with a clear message before any spawn — not an opaque crash.
    _use(monkeypatch, _StubInvoker("unused", remediation="set ANTHROPIC_API_KEY first"))
    with pytest.raises(SpawnContractError, match="ANTHROPIC_API_KEY"):
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

    from hpc_agent._wire.spawn_contract import WorkerReport
    from hpc_agent.cli.spawn import cmd_run

    monkeypatch.delenv("HPC_AGENT_INLINE", raising=False)
    report = WorkerReport(result={"run_id": "r1"}, anomalies="")
    # cmd_run does `from hpc_agent._kernel.lifecycle.run import run_workflow`
    # at call time, so patch the symbol there (its canonical home).
    monkeypatch.setattr(
        "hpc_agent._kernel.lifecycle.run.run_workflow",
        lambda **kwargs: (report, 0),
    )
    rc = cmd_run(argparse.Namespace(workflow="submit", experiment_dir=Path("."), fields_json="{}"))
    assert rc == 0
    env = json.loads(capsys.readouterr().out.strip())
    assert env["ok"] is True
    assert env["data"]["mode"] == "spawn"
    assert env["data"]["report"]["result"] == {"run_id": "r1"}
    assert env["data"]["worker_exit_code"] == 0


def _no_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make run_workflow explode so an inline test proves nothing spawned."""

    def _boom(**kwargs: object) -> object:
        raise AssertionError("inline mode must not call run_workflow / spawn a worker")

    monkeypatch.setattr("hpc_agent._kernel.lifecycle.run.run_workflow", _boom)


def test_cmd_run_inline_flag_renders_without_spawning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse
    import json

    from hpc_agent.cli.spawn import cmd_run

    monkeypatch.delenv("HPC_AGENT_INLINE", raising=False)
    _no_spawn(monkeypatch)
    rc = cmd_run(
        argparse.Namespace(
            workflow="submit", experiment_dir=Path("/exp"), fields_json="{}", inline=True
        )
    )
    assert rc == 0
    env = json.loads(capsys.readouterr().out.strip())
    assert env["ok"] is True
    assert env["data"]["mode"] == "inline"
    assert env["data"]["workflow"] == "submit"
    assert env["data"]["experiment_dir"] == "/exp"
    # The rendered prompt is the canonical procedure the worker would have run.
    assert "submit" in env["data"]["prompt"]
    assert env["data"]["prompt"].strip()
    assert "instructions" in env["data"]


def test_cmd_run_inline_via_env(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse
    import json

    from hpc_agent.cli.spawn import cmd_run

    monkeypatch.setenv("HPC_AGENT_INLINE", "1")
    _no_spawn(monkeypatch)
    # No `inline` attr on the namespace — the env knob alone flips the mode.
    rc = cmd_run(argparse.Namespace(workflow="status", experiment_dir=Path("."), fields_json="{}"))
    assert rc == 0
    env = json.loads(capsys.readouterr().out.strip())
    assert env["data"]["mode"] == "inline"


def test_cmd_run_inline_rejects_bad_fields_json(monkeypatch: pytest.MonkeyPatch) -> None:
    # Field validation runs before the inline branch, so bad JSON still fails fast.
    import argparse

    from hpc_agent.cli.spawn import cmd_run

    monkeypatch.setenv("HPC_AGENT_INLINE", "1")
    rc = cmd_run(
        argparse.Namespace(workflow="submit", experiment_dir=Path("."), fields_json="not-json")
    )
    assert rc == 1
