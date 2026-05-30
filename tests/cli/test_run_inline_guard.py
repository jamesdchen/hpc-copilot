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


def test_inline_instructions_route_to_pinned_subagent_then_fall_back(tmp_path, monkeypatch, capsys):
    """Inline mode must offer a three-tier capability ladder: the haiku-pinned
    named subagent `hpc-worker` first, a generic subagent (model-hinted) next,
    in-context last — and never re-spawn a `claude -p` worker. The prose +
    structured `subagent` hint are the cross-harness contract, and directives on
    this path have regressed before (the #155 guard), so they are pinned.
    """
    from hpc_agent.cli import spawn

    # Drive inline via the env opt-in so the #155 worker-available guard is
    # bypassed without needing to mock credentials; the instructions text is
    # static, so stub the (heavy) prompt render.
    monkeypatch.setenv("HPC_AGENT_INVOKER", "inline")
    monkeypatch.setattr(
        "hpc_agent._kernel.extension.spawn_prompt.validate_and_render_parts",
        lambda _: types.SimpleNamespace(joined="PROMPT-BODY"),
    )
    rc = spawn.cmd_run(_args(tmp_path, inline=False))
    env = _envelope(capsys)
    assert rc == 0
    assert env["data"]["mode"] == "inline"

    # Structured routing hint — a harness dispatches off this without parsing
    # prose. The model pin rides with the named definition; the field mirrors it.
    sub = env["data"]["subagent"]
    assert sub["preferred_name"] == "hpc-worker"
    assert sub["model"] == "haiku"
    assert sub["task"] == env["data"]["prompt"]

    instr = env["data"]["instructions"]
    low = instr.lower()
    # Tier 1: the pinned named subagent, named explicitly.
    assert "hpc-worker" in low
    # The pin is enforced by the harness from the definition — don't override.
    assert "haiku" in low or "small, cheap model" in low
    # Tier 2: generic subagent tool, by current + former token.
    assert "`agent` tool" in low and "task" in low
    # Tier 3: a real in-context fallback — inline is NOT subsumed by the subagent path.
    assert "yourself in this session" in low
    # The subagent (when used) is the leaf; must not route back into a spawn.
    assert "leaf" in low
    assert "hpc-agent run" in instr
    # The worker-report contract is still stated verbatim.
    assert "result" in instr and "decisions" in instr and "anomalies" in instr
    # Isolation ceiling (option 3): the prose must NOT over-promise — it names
    # that a subagent recovers context but not environment isolation (sandbox /
    # CLAUDE.md), and points a user who needs the latter at the default spawn.
    assert "isolation ceiling" in low
    assert "sandbox" in low and "claude.md" in low
    assert "environment" in low
