"""Worker-invoker transport seam."""

from __future__ import annotations

from pathlib import Path

import pytest

import hpc_agent._kernel.lifecycle.invoke as invoke_mod
from hpc_agent import errors
from hpc_agent._kernel.lifecycle.invoke import (
    ClaudeCliInvoker,
    InvocationResult,
    RenderedPrompt,
    get_invoker,
)


def test_get_invoker_default() -> None:
    assert get_invoker().name == "claude-cli"


def test_get_invoker_unknown_raises() -> None:
    with pytest.raises(errors.SpecInvalid, match="unknown worker invoker"):
        get_invoker("does-not-exist")


def test_get_invoker_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_AGENT_INVOKER", "claude-cli")
    assert get_invoker().name == "claude-cli"
    monkeypatch.setenv("HPC_AGENT_INVOKER", "bogus")
    with pytest.raises(errors.SpecInvalid, match="unknown worker invoker"):
        get_invoker()


def test_claude_cli_invoker_builds_the_right_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, object] = {}

    class _Proc:
        returncode = 0
        stdout = "worker output"

    def _fake_run(argv: list[str], **kwargs: object) -> _Proc:
        seen["argv"] = argv
        seen["cwd"] = kwargs.get("cwd")
        return _Proc()

    monkeypatch.setattr(invoke_mod.subprocess, "run", _fake_run)

    prompt = RenderedPrompt(cacheable_prefix="PREFIX", variable_suffix="SUFFIX")
    result = ClaudeCliInvoker().invoke(prompt, cwd=tmp_path)

    assert isinstance(result, InvocationResult)
    assert result.exit_code == 0
    assert result.output == "worker output"
    # The cacheable prefix is conveyed via --append-system-prompt (Claude
    # Code caches the system prompt); the variable suffix is the user prompt.
    assert seen["argv"] == [
        "claude",
        "-p",
        "--bare",
        "--append-system-prompt",
        "PREFIX",
        "SUFFIX",
    ]
    assert seen["cwd"] == str(tmp_path)


def test_rendered_prompt_joined() -> None:
    assert RenderedPrompt("A", "B").joined == "A\n\nB"
