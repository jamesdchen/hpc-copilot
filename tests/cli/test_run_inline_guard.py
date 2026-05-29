"""``hpc-agent run`` inline guard (#155).

The agent-reachable ``--inline`` flag must not let a caller synthesize an
in-context run when a spawning worker can authenticate — inline trades away the
worker's context isolation and is a USER opt-in (``HPC_AGENT_INVOKER=inline``),
not an agent default. The prose directive proved insufficient (the agent forced
``--inline`` anyway, twice), so this pins the hard CLI guard and its escape
hatches.
"""

from __future__ import annotations

import argparse
import json
import types

import pytest


def _args(tmp_path, *, inline=False, workflow="submit", fields="{}"):
    return argparse.Namespace(
        workflow=workflow,
        experiment_dir=tmp_path,
        fields_json=fields,
        inline=inline,
    )


def _envelope(capsys):
    out = capsys.readouterr().out.strip().splitlines()
    return json.loads(out[-1])


@pytest.fixture(autouse=True)
def _no_env_inline(monkeypatch):
    """Default: HPC_AGENT_INVOKER unset so each test states the env opt-in itself."""
    monkeypatch.delenv("HPC_AGENT_INVOKER", raising=False)


def test_inline_flag_refused_when_worker_available(tmp_path, monkeypatch, capsys):
    from hpc_agent.cli import spawn

    monkeypatch.setattr(
        "hpc_agent._kernel.lifecycle.invoke.worker_credentials_available", lambda: True
    )

    # The spawn path must NOT run — the guard returns before reaching it.
    def _boom(**_):
        raise AssertionError("run_workflow must not be called when --inline is refused")

    monkeypatch.setattr("hpc_agent._kernel.lifecycle.run.run_workflow", _boom)

    rc = spawn.cmd_run(_args(tmp_path, inline=True))
    env = _envelope(capsys)
    assert rc == 1
    assert env["ok"] is False
    assert env["error_code"] == "spec_invalid"
    assert "not honored" in env["message"]
    assert "HPC_AGENT_INVOKER=inline" in env["message"]


def test_inline_flag_honored_when_no_worker(tmp_path, monkeypatch, capsys):
    """With no auto-selectable worker, inline IS the only option — still honored."""
    from hpc_agent.cli import spawn

    monkeypatch.setattr(
        "hpc_agent._kernel.lifecycle.invoke.worker_credentials_available", lambda: False
    )
    monkeypatch.setattr(
        "hpc_agent._kernel.extension.spawn_prompt.validate_and_render_parts",
        lambda _: types.SimpleNamespace(joined="PROMPT-BODY"),
    )
    rc = spawn.cmd_run(_args(tmp_path, inline=True))
    env = _envelope(capsys)
    assert rc == 0
    assert env["ok"] is True
    assert env["data"]["mode"] == "inline"
    assert env["data"]["prompt"] == "PROMPT-BODY"


def test_env_inline_honored_even_with_worker(tmp_path, monkeypatch, capsys):
    """``HPC_AGENT_INVOKER=inline`` is the user's deliberate opt-in — never refused."""
    from hpc_agent.cli import spawn

    monkeypatch.setenv("HPC_AGENT_INVOKER", "inline")
    monkeypatch.setattr(
        "hpc_agent._kernel.lifecycle.invoke.worker_credentials_available", lambda: True
    )
    monkeypatch.setattr(
        "hpc_agent._kernel.extension.spawn_prompt.validate_and_render_parts",
        lambda _: types.SimpleNamespace(joined="PROMPT-BODY"),
    )
    # The --inline flag is NOT set; the env var alone drives inline, bypassing the guard.
    rc = spawn.cmd_run(_args(tmp_path, inline=False))
    env = _envelope(capsys)
    assert rc == 0
    assert env["ok"] is True
    assert env["data"]["mode"] == "inline"


def test_default_still_spawns(tmp_path, monkeypatch, capsys):
    """No flag, no env → the default spawn path, unaffected by the guard."""
    from hpc_agent.cli import spawn

    report = types.SimpleNamespace(model_dump=lambda: {"result": "ok"})
    monkeypatch.setattr("hpc_agent._kernel.lifecycle.run.run_workflow", lambda **_: (report, 0))
    rc = spawn.cmd_run(_args(tmp_path, inline=False))
    env = _envelope(capsys)
    assert rc == 0
    assert env["ok"] is True
    assert env["data"]["mode"] == "spawn"
    assert env["data"]["worker_exit_code"] == 0


def test_worker_credentials_available_reads_env_and_oauth(monkeypatch):
    from hpc_agent._kernel.lifecycle import invoke

    monkeypatch.setattr(invoke, "_oauth_credentials_available", lambda: False)
    for var in invoke._WORKER_CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    assert invoke.worker_credentials_available() is False

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert invoke.worker_credentials_available() is True

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(invoke, "_oauth_credentials_available", lambda: True)
    assert invoke.worker_credentials_available() is True
