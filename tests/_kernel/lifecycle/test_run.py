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

    def invoke(
        self, prompt: RenderedPrompt, *, cwd: Path, report_cache_stats: bool = False
    ) -> InvocationResult:
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
    report, code, cache_stats = run_workflow(
        workflow="submit", experiment_dir=".", fields={"cluster": "sge1"}
    )
    assert code == 0
    assert report.result == {"run_id": "r1"}
    assert report.decisions[0].point == "canary"
    # No cache stats requested → None (the default path is untouched).
    assert cache_stats is None


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


def test_run_workflow_crash_message_mentions_inline_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The malformed-report error surfaces HPC_AGENT_INVOKER=inline as a
    fallback. The spawned worker holds its own credential (ANTHROPIC_API_KEY
    for a `--bare` child), separate from the caller's interactive session;
    when that key hits quota/billing limits, the worker dies before
    producing a report — exactly this code path. Inline mode skips the
    spawn and runs in the caller's session, so it's the natural recovery
    for the quota class. Always-on hint (sentence of prose, no harm
    when the failure is unrelated)."""
    _use(monkeypatch, _StubInvoker("boom — no json here", exit_code=3))
    with pytest.raises(errors.HpcError) as excinfo:
        run_workflow(workflow="submit", experiment_dir=".", fields={})
    msg = str(excinfo.value)
    assert "HPC_AGENT_INVOKER=inline" in msg
    assert "fallback" in msg.lower()
    assert "workspace API-quota" in msg or "quota" in msg.lower()


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

    monkeypatch.delenv("HPC_AGENT_INVOKER", raising=False)
    report = WorkerReport(result={"run_id": "r1"}, anomalies="")
    # cmd_run does `from hpc_agent._kernel.lifecycle.run import run_workflow`
    # at call time, so patch the symbol there (its canonical home).
    monkeypatch.setattr(
        "hpc_agent._kernel.lifecycle.run.run_workflow",
        lambda **kwargs: (report, 0, None),
    )
    rc = cmd_run(argparse.Namespace(workflow="submit", experiment_dir=Path("."), fields_json="{}"))
    assert rc == 0
    env = json.loads(capsys.readouterr().out.strip())
    assert env["ok"] is True
    assert env["data"]["mode"] == "spawn"
    assert env["data"]["report"]["result"] == {"run_id": "r1"}
    assert env["data"]["worker_exit_code"] == 0
    # Cache-stat monitoring is on by default now (#244): the key is always
    # present, None here since the stub reports no usage.
    assert env["data"]["cache_stats"] is None


def test_cmd_run_reports_cache_stats_by_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # #244 always-on: with no flag and no env opt-out, cmd_run asks
    # run_workflow for cache stats and surfaces them in the envelope.
    import argparse
    import json

    from hpc_agent._wire.spawn_contract import WorkerReport
    from hpc_agent.cli.spawn import cmd_run

    monkeypatch.delenv("HPC_AGENT_INVOKER", raising=False)
    monkeypatch.delenv("HPC_AGENT_REPORT_CACHE_STATS", raising=False)
    report = WorkerReport(result={"run_id": "r1"}, anomalies="")
    captured: dict[str, object] = {}

    def _fake(**kwargs: object) -> object:
        captured.update(kwargs)
        return (report, 0, {"cache_read_input_tokens": 4000, "cache_creation_input_tokens": 12})

    monkeypatch.setattr("hpc_agent._kernel.lifecycle.run.run_workflow", _fake)
    rc = cmd_run(argparse.Namespace(workflow="submit", experiment_dir=Path("."), fields_json="{}"))
    assert rc == 0
    assert captured["report_cache_stats"] is True
    env = json.loads(capsys.readouterr().out.strip())
    assert env["data"]["cache_stats"] == {
        "cache_read_input_tokens": 4000,
        "cache_creation_input_tokens": 12,
    }


def test_cmd_run_cache_stats_env_opt_out(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # HPC_AGENT_REPORT_CACHE_STATS=0 disables the monitoring (and the
    # --output-format json transport): run_workflow is asked NOT to report,
    # and no cache_stats key appears.
    import argparse
    import json

    from hpc_agent._wire.spawn_contract import WorkerReport
    from hpc_agent.cli.spawn import cmd_run

    monkeypatch.delenv("HPC_AGENT_INVOKER", raising=False)
    monkeypatch.setenv("HPC_AGENT_REPORT_CACHE_STATS", "0")
    report = WorkerReport(result={"run_id": "r1"}, anomalies="")
    captured: dict[str, object] = {}

    def _fake(**kwargs: object) -> object:
        captured.update(kwargs)
        return (report, 0, None)

    monkeypatch.setattr("hpc_agent._kernel.lifecycle.run.run_workflow", _fake)
    rc = cmd_run(argparse.Namespace(workflow="submit", experiment_dir=Path("."), fields_json="{}"))
    assert rc == 0
    assert captured["report_cache_stats"] is False
    env = json.loads(capsys.readouterr().out.strip())
    assert "cache_stats" not in env["data"]


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

    monkeypatch.delenv("HPC_AGENT_INVOKER", raising=False)
    # The #155 guard refuses an agent-supplied --inline when a spawning worker
    # could authenticate; this test exercises the no-worker fallback where
    # --inline IS the only path, so pin that precondition (otherwise the result
    # leaks the ambient ANTHROPIC_API_KEY / OAuth login of the host).
    monkeypatch.setattr(
        "hpc_agent._kernel.lifecycle.invoke.worker_credentials_available", lambda: False
    )
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
    assert env["data"]["experiment_dir"] == str(Path("/exp"))
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

    monkeypatch.setenv("HPC_AGENT_INVOKER", "inline")
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

    monkeypatch.setenv("HPC_AGENT_INVOKER", "inline")
    rc = cmd_run(
        argparse.Namespace(workflow="submit", experiment_dir=Path("."), fields_json="not-json")
    )
    assert rc == 1


def test_cmd_run_reads_fields_from_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """--fields-file is read + parsed — the Windows escape-hatch for inline
    JSON that a shell quoting layer would mangle (backslash paths)."""
    import argparse
    import json

    from hpc_agent.cli.spawn import cmd_run

    monkeypatch.setenv("HPC_AGENT_INVOKER", "inline")
    _no_spawn(monkeypatch)
    fields_file = tmp_path / "fields.json"
    fields_file.write_text("{}", encoding="utf-8")
    rc = cmd_run(
        argparse.Namespace(
            workflow="submit",
            experiment_dir=Path("."),
            fields_json="{}",
            fields_file=str(fields_file),
        )
    )
    assert rc == 0
    env = json.loads(capsys.readouterr().out.strip())
    assert env["data"]["mode"] == "inline"


def test_cmd_run_fields_file_wins_and_labels_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """--fields-file takes precedence over --fields-json, and a malformed file
    fails fast with a message naming the file source (not --fields-json)."""
    import argparse
    import json

    from hpc_agent.cli.spawn import cmd_run

    monkeypatch.setenv("HPC_AGENT_INVOKER", "inline")
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid", encoding="utf-8")
    rc = cmd_run(
        argparse.Namespace(
            workflow="submit",
            experiment_dir=Path("."),
            fields_json="{}",  # valid, but the file wins → the file's bad JSON surfaces
            fields_file=str(bad),
        )
    )
    assert rc == 1
    env = json.loads(capsys.readouterr().out.strip())
    assert env["error_code"] == "spec_invalid"
    assert "--fields-file" in env["message"]
