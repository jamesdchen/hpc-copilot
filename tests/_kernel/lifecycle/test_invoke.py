"""Worker-invoker transport seam."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import hpc_agent._kernel.lifecycle.invoke as invoke_mod
from hpc_agent import errors
from hpc_agent._kernel.lifecycle.invoke import (
    ClaudeCliInvoker,
    ClaudeCliOAuthInvoker,
    InvocationResult,
    RenderedPrompt,
    get_invoker,
)


def _clear_worker_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every ambient credential signal so auto-selection is deterministic."""
    for var in (*invoke_mod._WORKER_CREDENTIAL_ENV_VARS, "HPC_AGENT_INVOKER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(invoke_mod, "_oauth_credentials_available", lambda: False)


def test_get_invoker_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # No credentials anywhere → the proven --bare path, whose pre-spawn guard
    # then surfaces the missing-credential remediation.
    _clear_worker_credentials(monkeypatch)
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


def _capture_run(seen: dict[str, object]):
    """A fake ``subprocess.run`` that records argv/cwd/env/stdin and, while the
    temp dir still exists, the content of the ``--append-system-prompt-file``."""

    class _Proc:
        returncode = 0
        stdout = "worker output"
        stderr = ""

    def _fake_run(argv: list[str], **kwargs: object) -> _Proc:
        seen["argv"] = argv
        seen["cwd"] = kwargs.get("cwd")
        seen["env"] = kwargs.get("env")
        seen["input"] = kwargs.get("input")
        # The system-prompt temp file exists during the call (the TemporaryDirectory
        # is cleaned up only after invoke() returns), so read it here.
        idx = argv.index("--append-system-prompt-file") + 1
        seen["system_prompt"] = Path(argv[idx]).read_text(encoding="utf-8")
        return _Proc()

    return _fake_run


def test_claude_cli_invoker_builds_the_right_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(invoke_mod.subprocess, "run", _capture_run(seen))

    prompt = RenderedPrompt(cacheable_prefix="PREFIX", variable_suffix="SUFFIX")
    result = ClaudeCliInvoker().invoke(prompt, cwd=tmp_path)

    assert isinstance(result, InvocationResult)
    assert result.exit_code == 0
    assert result.output == "worker output"
    # The cacheable prefix is conveyed via --append-system-prompt-file (Claude
    # Code caches the system prompt); the variable suffix is fed on stdin as the
    # user prompt. Both are kept OFF argv so the rendered prompt can't overrun
    # Windows' 32K command-length limit (#169). The worker forces the sandbox off
    # (it SSH/rsyncs to a cluster — network the sandbox blocks, and native
    # Windows can't sandbox at all).
    argv = seen["argv"]
    assert isinstance(argv, list)
    assert argv[:-1] == [
        "claude",
        "-p",
        "--bare",
        "--model",
        "haiku",
        "--settings",
        '{"sandbox": {"enabled": false}}',
        "--allowedTools",
        invoke_mod._WORKER_ALLOWED_TOOLS,
        "--disallowedTools",
        invoke_mod._WORKER_DISALLOWED_TOOLS,
        "--append-system-prompt-file",
    ]
    assert seen["system_prompt"] == "PREFIX"
    assert seen["input"] == "SUFFIX"
    assert seen["cwd"] == str(tmp_path)


def test_invoker_keeps_large_prompt_off_the_command_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # #169: native Windows' CreateProcessW caps the WHOLE command line at 32,767
    # chars. The rendered submit worker prompt is tens of KB, so neither the
    # cacheable prefix nor the variable suffix may ride argv — the prefix goes to
    # a temp file (--append-system-prompt-file) and the suffix goes on stdin.
    seen: dict[str, object] = {}
    monkeypatch.setattr(invoke_mod.subprocess, "run", _capture_run(seen))

    big_prefix = "P" * 50_000
    big_suffix = "S" * 50_000
    ClaudeCliInvoker().invoke(
        RenderedPrompt(cacheable_prefix=big_prefix, variable_suffix=big_suffix), cwd=tmp_path
    )

    argv = seen["argv"]
    assert isinstance(argv, list)
    # Every argv element stays tiny — nothing approaches the 32K limit.
    assert max(len(a) for a in argv) < 1_000
    assert big_prefix not in argv
    assert big_suffix not in argv
    # The content still reaches the worker, just off-argv.
    assert seen["system_prompt"] == big_prefix
    assert seen["input"] == big_suffix


def test_rendered_prompt_joined() -> None:
    assert RenderedPrompt("A", "B").joined == "A\n\nB"


def test_missing_credential_remediation_none_when_api_key_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-something")
    assert ClaudeCliInvoker().missing_credential_remediation() is None


def test_missing_credential_remediation_message_when_no_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An OAuth-only parent session: none of the API-key / bearer / cloud-provider
    # env vars are set, so the bare worker has no usable credential.
    for var in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
    ):
        monkeypatch.delenv(var, raising=False)
    msg = ClaudeCliInvoker().missing_credential_remediation()
    assert msg is not None
    assert "ANTHROPIC_API_KEY" in msg


# ─── auto-selection ────────────────────────────────────────────────────────


def test_get_invoker_auto_selects_claude_cli_with_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_worker_credentials(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    assert get_invoker().name == "claude-cli"


def test_get_invoker_auto_selects_oauth_when_only_creds_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No API key / cloud creds, but an OAuth credentials file is present.
    _clear_worker_credentials(monkeypatch)
    monkeypatch.setattr(invoke_mod, "_oauth_credentials_available", lambda: True)
    assert get_invoker().name == "claude-cli-oauth"


def test_get_invoker_inline_is_not_a_spawning_transport() -> None:
    # "inline" is a valid HPC_AGENT_INVOKER value but `hpc-agent run` intercepts
    # it; a spawn path that reaches get_invoker must reject it with a clear hint.
    with pytest.raises(errors.SpecInvalid, match="in-context execution"):
        get_invoker("inline")


def test_get_invoker_env_override_beats_auto_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An OAuth creds file would auto-select claude-cli-oauth, but an explicit
    # HPC_AGENT_INVOKER wins.
    _clear_worker_credentials(monkeypatch)
    monkeypatch.setattr(invoke_mod, "_oauth_credentials_available", lambda: True)
    monkeypatch.setenv("HPC_AGENT_INVOKER", "claude-cli")
    assert get_invoker().name == "claude-cli"
    # An explicit name beats both.
    assert get_invoker("claude-cli-oauth").name == "claude-cli-oauth"


# ─── OAuth invoker ─────────────────────────────────────────────────────────


def test_oauth_credentials_path_uses_config_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(invoke_mod.sys, "platform", "linux")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert invoke_mod._oauth_credentials_path() == tmp_path / ".credentials.json"


def test_oauth_credentials_path_none_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    # macOS keeps the OAuth token in the Keychain — no linkable file.
    monkeypatch.setattr(invoke_mod.sys, "platform", "darwin")
    assert invoke_mod._oauth_credentials_path() is None


def test_oauth_invoker_builds_the_right_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    creds = tmp_path / ".credentials.json"
    creds.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(invoke_mod, "_oauth_credentials_path", lambda: creds)

    linked: dict[str, Path] = {}
    monkeypatch.setattr(
        invoke_mod,
        "_link_credentials",
        lambda live, link: linked.update(live=live, link=link),
    )

    seen: dict[str, object] = {}
    monkeypatch.setattr(invoke_mod.subprocess, "run", _capture_run(seen))

    prompt = RenderedPrompt(cacheable_prefix="PREFIX", variable_suffix="SUFFIX")
    exp_dir = tmp_path / "exp"
    result = ClaudeCliOAuthInvoker().invoke(prompt, cwd=exp_dir)

    assert isinstance(result, InvocationResult)
    assert result.exit_code == 0
    assert result.output == "worker output"
    # No --bare (it strips the OAuth-credential path); same model + sandbox-off +
    # off-argv prompt transport (system-prompt file + stdin) as the --bare path.
    argv = seen["argv"]
    assert isinstance(argv, list)
    assert argv[:-1] == [
        "claude",
        "-p",
        "--model",
        "haiku",
        "--settings",
        '{"sandbox": {"enabled": false}}',
        "--allowedTools",
        invoke_mod._WORKER_ALLOWED_TOOLS,
        "--disallowedTools",
        invoke_mod._WORKER_DISALLOWED_TOOLS,
        "--append-system-prompt-file",
    ]
    assert seen["system_prompt"] == "PREFIX"
    assert seen["input"] == "SUFFIX"
    # The child is pointed at an ephemeral CLAUDE_CONFIG_DIR holding only the
    # linked creds, and runs in a clean cwd — NOT the experiment dir — so a user
    # repo's project .claude/ never loads into the worker.
    env = seen["env"]
    assert isinstance(env, dict)
    config_dir = env["CLAUDE_CONFIG_DIR"]
    assert config_dir
    assert linked["live"] == creds
    assert linked["link"] == Path(config_dir) / ".credentials.json"
    assert seen["cwd"] != str(exp_dir)
    assert seen["cwd"] != config_dir


def _disallowed_after_flag(argv: list[str]) -> str:
    """The single value passed to ``--disallowedTools`` in *argv*."""
    return argv[argv.index("--disallowedTools") + 1]


def test_worker_spawn_fences_destructive_cluster_ops(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both spawn paths must carry the destructive-op deny that `--bare` and the
    OAuth ephemeral config drop from settings.json. Job submission/cancellation
    belongs to `submit-flow`; hpc-agent has no kill verb by design."""
    # --bare path
    seen_bare: dict[str, object] = {}
    monkeypatch.setattr(invoke_mod.subprocess, "run", _capture_run(seen_bare))
    ClaudeCliInvoker().invoke(
        RenderedPrompt(cacheable_prefix="P", variable_suffix="S"), cwd=tmp_path
    )
    bare_argv = seen_bare["argv"]
    assert isinstance(bare_argv, list)

    # OAuth path
    creds = tmp_path / ".credentials.json"
    creds.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(invoke_mod, "_oauth_credentials_path", lambda: creds)
    monkeypatch.setattr(invoke_mod, "_link_credentials", lambda live, link: None)
    seen_oauth: dict[str, object] = {}
    monkeypatch.setattr(invoke_mod.subprocess, "run", _capture_run(seen_oauth))
    ClaudeCliOAuthInvoker().invoke(
        RenderedPrompt(cacheable_prefix="P", variable_suffix="S"), cwd=tmp_path / "exp"
    )
    oauth_argv = seen_oauth["argv"]
    assert isinstance(oauth_argv, list)

    # Both spawns carry the same strict fence: a default-deny allowlist
    # (only hpc-agent + git Bash, plus read/write/search tools) AND a
    # defense-in-depth denylist for cluster transport / scheduler / exfil.
    for argv in (bare_argv, oauth_argv):
        assert "--allowedTools" in argv
        allowed = argv[argv.index("--allowedTools") + 1]
        assert allowed == invoke_mod._WORKER_ALLOWED_TOOLS
        assert "Bash(hpc-agent:*)" in allowed
        assert "Bash(git:*)" in allowed
        # The worker can shell nothing else — no bare `Bash`, no python.
        assert "Bash(python" not in allowed and " Bash " not in f" {allowed} "

        assert "--disallowedTools" in argv
        fenced = _disallowed_after_flag(argv)
        assert fenced == invoke_mod._WORKER_DISALLOWED_TOOLS
        for op in ("scancel", "qdel", "qsub", "sbatch", "ssh", "rsync", "scp"):
            assert f"Bash({op}:*)" in fenced


def test_oauth_invoker_returns_failure_without_spawning_when_creds_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing = tmp_path / "nope" / ".credentials.json"
    monkeypatch.setattr(invoke_mod, "_oauth_credentials_path", lambda: missing)

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("must not spawn a worker without credentials")

    monkeypatch.setattr(invoke_mod.subprocess, "run", _boom)

    result = ClaudeCliOAuthInvoker().invoke(RenderedPrompt("P", "S"), cwd=tmp_path)
    assert result.exit_code == 1
    assert str(missing) in result.stderr


def test_oauth_missing_credential_remediation_none_when_file_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    creds = tmp_path / ".credentials.json"
    creds.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(invoke_mod, "_oauth_credentials_path", lambda: creds)
    assert ClaudeCliOAuthInvoker().missing_credential_remediation() is None


def test_oauth_missing_credential_remediation_message_when_file_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing = tmp_path / "nope" / ".credentials.json"
    monkeypatch.setattr(invoke_mod, "_oauth_credentials_path", lambda: missing)
    msg = ClaudeCliOAuthInvoker().missing_credential_remediation()
    assert msg is not None
    assert str(missing) in msg


def test_oauth_unsupported_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    # _oauth_credentials_path returns None on macOS → a clear unsupported message.
    monkeypatch.setattr(invoke_mod, "_oauth_credentials_path", lambda: None)
    msg = ClaudeCliOAuthInvoker().missing_credential_remediation()
    assert msg is not None
    assert "macOS" in msg


def test_link_credentials_prefers_symlink(tmp_path: Path) -> None:
    live = tmp_path / "live.json"
    live.write_text("tok", encoding="utf-8")
    link = tmp_path / "cfg" / ".credentials.json"
    link.parent.mkdir()

    invoke_mod._link_credentials(live, link)

    assert link.exists()
    assert link.read_text(encoding="utf-8") == "tok"
    if sys.platform != "win32":
        # Symlink (not copy) so a mid-session OAuth token refresh that rewrites
        # the live file is seen through the link.
        assert link.is_symlink()
        live.write_text("refreshed", encoding="utf-8")
        assert link.read_text(encoding="utf-8") == "refreshed"
