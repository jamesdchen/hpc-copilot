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
    CodexCliInvoker,
    GeminiCliInvoker,
    InvocationResult,
    RenderedPrompt,
    get_invoker,
)


def _clear_worker_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every ambient credential signal so auto-selection is deterministic."""
    for var in (
        *invoke_mod._WORKER_CREDENTIAL_ENV_VARS,
        invoke_mod._CODEX_CREDENTIAL_ENV_VAR,
        *invoke_mod._GEMINI_CREDENTIAL_ENV_VARS,
        "HPC_AGENT_INVOKER",
    ):
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
    monkeypatch.delenv("HPC_AGENT_WORKER_JSON_SCHEMA", raising=False)
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
    # Windows can't sandbox at all). The decode-time --json-schema constraint
    # rides the default spawn since the #269 flip.
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
        "--output-format",
        "json",
        "--json-schema",
        invoke_mod._worker_output_schema(),
        "--append-system-prompt-file",
    ]
    assert seen["system_prompt"] == "PREFIX"
    assert seen["input"] == "SUFFIX"
    assert seen["cwd"] == str(tmp_path)


def test_plain_transport_when_schema_gate_opted_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # With the decode-schema gate opted off (HPC_AGENT_WORKER_JSON_SCHEMA=0,
    # the documented fallback) and no report_cache_stats, the call carries NO
    # --output-format json and surfaces no cache_stats — the original text
    # transport is preserved.
    monkeypatch.setenv("HPC_AGENT_WORKER_JSON_SCHEMA", "0")
    seen: dict[str, object] = {}
    monkeypatch.setattr(invoke_mod.subprocess, "run", _capture_run(seen))
    result = ClaudeCliInvoker().invoke(
        RenderedPrompt(cacheable_prefix="P", variable_suffix="S"), cwd=tmp_path
    )
    argv = seen["argv"]
    assert isinstance(argv, list)
    assert "--output-format" not in argv
    assert result.output == "worker output"
    assert result.cache_stats is None


def _json_envelope_run(seen: dict[str, object], envelope: str):
    """A fake subprocess.run that records argv and returns *envelope* as stdout."""

    class _Proc:
        returncode = 0
        stdout = envelope
        stderr = ""

    def _fake_run(argv: list[str], **kwargs: object) -> _Proc:
        seen["argv"] = argv
        return _Proc()

    return _fake_run


def test_report_cache_stats_unwraps_envelope_and_surfaces_usage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # With report_cache_stats the worker runs with --output-format json; we
    # lift the report text out of `result` and the cache token counts out of
    # `usage`.
    import json

    seen: dict[str, object] = {}
    envelope = json.dumps(
        {
            "type": "result",
            "result": '{"result": {"run_id": "r1"}, "decisions": [], "anomalies": ""}',
            "usage": {
                "input_tokens": 12,
                "output_tokens": 34,
                "cache_creation_input_tokens": 4096,
                "cache_read_input_tokens": 4000,
            },
        }
    )
    monkeypatch.setattr(invoke_mod.subprocess, "run", _json_envelope_run(seen, envelope))

    result = ClaudeCliInvoker().invoke(
        RenderedPrompt(cacheable_prefix="P", variable_suffix="S"),
        cwd=tmp_path,
        report_cache_stats=True,
    )

    argv = seen["argv"]
    assert isinstance(argv, list)
    assert "--output-format" in argv and "json" in argv
    # The worker report text is the unwrapped inner `result`, not the envelope.
    assert result.output == '{"result": {"run_id": "r1"}, "decisions": [], "anomalies": ""}'
    assert result.cache_stats == {
        "input_tokens": 12,
        "output_tokens": 34,
        "cache_creation_input_tokens": 4096,
        "cache_read_input_tokens": 4000,
    }


def test_report_cache_stats_tolerates_a_non_json_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A worker that dies before emitting the JSON envelope leaves raw stdout as
    # the output (so the report-parse still surfaces its last words) and
    # cache_stats None — no crash on the unwrap.
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        invoke_mod.subprocess, "run", _json_envelope_run(seen, "Not logged in. Goodbye.")
    )
    result = ClaudeCliInvoker().invoke(
        RenderedPrompt(cacheable_prefix="P", variable_suffix="S"),
        cwd=tmp_path,
        report_cache_stats=True,
    )
    assert result.output == "Not logged in. Goodbye."
    assert result.cache_stats is None


def test_extract_cache_stats_returns_none_without_usage() -> None:
    assert invoke_mod._extract_cache_stats({"result": "x"}) is None


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
    # Every argv element stays small — the one sizeable element is the fixed
    # ~2KB minified WorkerReport schema (a constant, nowhere near the 32K
    # limit); the unbounded prompt halves must never ride argv.
    schema = invoke_mod._worker_output_schema()
    assert max(len(a) for a in argv if a != schema) < 1_000
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
        "--output-format",
        "json",
        "--json-schema",
        invoke_mod._worker_output_schema(),
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


# ─── decode-time output constraint (--json-schema) ──────────────────────────


def test_worker_output_schema_on_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # Default flipped after the #269 live validation run
    # (scripts/validate_worker_json_schema.py): unset means ON, binding the
    # lenient shape the run confirmed claude accepts.
    monkeypatch.delenv("HPC_AGENT_WORKER_JSON_SCHEMA", raising=False)
    schema = invoke_mod._worker_output_schema()
    assert schema is not None
    assert '"additionalProperties":false' not in schema


def test_worker_output_schema_on_returns_minified_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HPC_AGENT_WORKER_JSON_SCHEMA", "1")
    schema = invoke_mod._worker_output_schema()
    assert schema is not None
    assert "WorkerReport" in schema and '"decisions"' in schema
    assert "\n" not in schema  # minified for argv
    # Claude binds the LENIENT worker.output.json, not the strict variant — the
    # deliberate #269 asymmetry (claude's strictness requirement is unconfirmed).
    assert '"additionalProperties":false' not in schema


def test_off_value_disables_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    # The documented off-switch on the default-on gate (#269).
    monkeypatch.setenv("HPC_AGENT_WORKER_JSON_SCHEMA", "0")
    assert invoke_mod._worker_output_schema() is None
    monkeypatch.setenv("HPC_AGENT_WORKER_JSON_SCHEMA", "false")
    assert invoke_mod._worker_output_schema() is None


def test_schema_constrained_invocation_adds_flags_and_unwraps_object(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import json

    monkeypatch.setenv("HPC_AGENT_WORKER_JSON_SCHEMA", "1")
    seen: dict[str, object] = {}
    # --json-schema makes the inner `result` a structured object; we re-serialize
    # it so parse_worker_report sees a JSON object on stdout either way.
    inner = {"result": {}, "decisions": [], "anomalies": "ok"}
    envelope = json.dumps({"type": "result", "result": inner})
    monkeypatch.setattr(invoke_mod.subprocess, "run", _json_envelope_run(seen, envelope))

    result = ClaudeCliInvoker().invoke(
        RenderedPrompt(cacheable_prefix="P", variable_suffix="S"), cwd=tmp_path
    )
    argv = seen["argv"]
    assert isinstance(argv, list)
    assert "--json-schema" in argv
    assert "--output-format" in argv and "json" in argv
    assert json.loads(result.output) == inner


def test_schema_constrained_unwraps_string_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import json

    monkeypatch.setenv("HPC_AGENT_WORKER_JSON_SCHEMA", "1")
    seen: dict[str, object] = {}
    inner = '{"result": {}, "decisions": [], "anomalies": "x"}'
    envelope = json.dumps({"type": "result", "result": inner})
    monkeypatch.setattr(invoke_mod.subprocess, "run", _json_envelope_run(seen, envelope))

    result = ClaudeCliInvoker().invoke(
        RenderedPrompt(cacheable_prefix="P", variable_suffix="S"), cwd=tmp_path
    )
    assert result.output == inner


# ─── Codex CLI invoker ──────────────────────────────────────────────────────


def _codex_capture_run(seen: dict[str, object], *, report: str = "codex report"):
    """A fake ``subprocess.run`` recording argv/cwd/env/stdin and writing the
    Codex ``--output-last-message`` file (Codex reads the report from there, not
    stdout). Also captures the execpolicy ``.rules`` body while the temp dir
    still exists."""

    class _Proc:
        returncode = 0
        stdout = "codex stdout (not the report)"
        stderr = ""

    def _fake_run(argv: list[str], **kwargs: object) -> _Proc:
        seen["argv"] = argv
        seen["cwd"] = kwargs.get("cwd")
        seen["env"] = kwargs.get("env")
        seen["input"] = kwargs.get("input")
        rules_path = argv[argv.index("--config") + 1].split("=", 1)[1]
        seen["rules"] = Path(rules_path).read_text(encoding="utf-8")
        last_idx = argv.index("--output-last-message") + 1
        Path(argv[last_idx]).write_text(report, encoding="utf-8")
        return _Proc()

    return _fake_run


def test_codex_cli_invoker_builds_the_right_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("HPC_AGENT_CODEX_OUTPUT_SCHEMA", raising=False)
    seen: dict[str, object] = {}
    monkeypatch.setattr(invoke_mod.subprocess, "run", _codex_capture_run(seen))

    prompt = RenderedPrompt(cacheable_prefix="PREFIX", variable_suffix="SUFFIX")
    result = CodexCliInvoker().invoke(prompt, cwd=tmp_path)

    assert isinstance(result, InvocationResult)
    assert result.exit_code == 0
    # The report is the --output-last-message file, NOT stdout.
    assert result.output == "codex report"
    # cache_stats not surfaced at the Codex CLI layer.
    assert result.cache_stats is None

    argv = seen["argv"]
    assert isinstance(argv, list)
    assert argv[0:4] == ["codex", "exec", "-m", "gpt-5.4-mini"]
    # Autonomy posture: full disk+net, zero prompts (worker SSH/rsyncs out).
    assert "--dangerously-bypass-approvals-and-sandbox" in argv
    # Schema accelerator OFF by default → no --output-schema.
    assert "--output-schema" not in argv
    # Whole prompt on stdin (no prefix/suffix split for Codex), nothing on argv.
    assert argv[-1] == "-"
    assert seen["input"] == prompt.joined
    assert "PREFIX" not in argv and "SUFFIX" not in argv
    assert seen["cwd"] == str(tmp_path)


def test_codex_execpolicy_rules_carry_the_full_cluster_op_deny(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(invoke_mod.subprocess, "run", _codex_capture_run(seen))
    CodexCliInvoker().invoke(RenderedPrompt("P", "S"), cwd=tmp_path)

    rules = seen["rules"]
    assert isinstance(rules, str)
    # Strictest-severity-wins: every cluster-op command is forbidden.
    for cmd in invoke_mod._CLUSTER_OP_DENY_COMMANDS:
        assert f'prefix_rule(pattern=["{cmd}"], decision="forbidden")' in rules
    # The cluster-op surface the #283/#228 invariant protects.
    for op in ("scancel", "qdel", "bkill", "ssh", "rsync", "scp", "curl", "wget"):
        assert op in rules


def test_codex_model_pin_overridable_by_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HPC_AGENT_CODEX_WORKER_MODEL", "gpt-5.4-custom")
    seen: dict[str, object] = {}
    monkeypatch.setattr(invoke_mod.subprocess, "run", _codex_capture_run(seen))
    CodexCliInvoker().invoke(RenderedPrompt("P", "S"), cwd=tmp_path)
    argv = seen["argv"]
    assert isinstance(argv, list)
    assert argv[argv.index("-m") + 1] == "gpt-5.4-custom"


def test_codex_output_schema_bound_when_flag_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HPC_AGENT_CODEX_OUTPUT_SCHEMA", "1")
    seen: dict[str, object] = {}

    captured: dict[str, str] = {}

    base = _codex_capture_run(seen)

    def _fake_run(argv: list[str], **kwargs: object):
        # Capture the schema file content while the temp dir still exists.
        idx = argv.index("--output-schema") + 1
        captured["schema"] = Path(argv[idx]).read_text(encoding="utf-8")
        return base(argv, **kwargs)

    monkeypatch.setattr(invoke_mod.subprocess, "run", _fake_run)
    CodexCliInvoker().invoke(RenderedPrompt("P", "S"), cwd=tmp_path)
    argv = seen["argv"]
    assert isinstance(argv, list)
    assert "--output-schema" in argv
    assert "WorkerReport" in captured["schema"]
    # Codex binds the API-STRICT variant, not the lenient floor schema.
    assert '"additionalProperties":false' in captured["schema"]
    assert '"required":["point","outcome","why","chosen","rejected"]' in captured["schema"]


def test_codex_output_schema_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HPC_AGENT_CODEX_OUTPUT_SCHEMA", raising=False)
    assert invoke_mod._codex_output_schema() is None


def test_codex_output_schema_on_returns_strict_minified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HPC_AGENT_CODEX_OUTPUT_SCHEMA", "1")
    schema = invoke_mod._codex_output_schema()
    assert schema is not None
    assert "WorkerReport" in schema
    assert "\n" not in schema  # minified
    # The strict variant: object roots forbid extras and require every field.
    assert '"additionalProperties":false' in schema
    assert '"required":["result","decisions","anomalies"]' in schema


def test_decode_schema_gates_are_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The split gate (#269): Claude's var must not turn Codex on, or vice versa.

    A single shared gate would flip an unvalidated harness on as a side effect
    of validating the other — the whole reason the gate was split.
    """
    # Claude on, Codex unset → only Claude's schema is bound.
    monkeypatch.setenv("HPC_AGENT_WORKER_JSON_SCHEMA", "1")
    monkeypatch.delenv("HPC_AGENT_CODEX_OUTPUT_SCHEMA", raising=False)
    assert invoke_mod._worker_output_schema() is not None
    assert invoke_mod._codex_output_schema() is None

    # Codex on, Claude opted off → only Codex's schema is bound. (Claude's
    # gate defaults ON post-flip, so the independence probe uses the explicit
    # off-switch.)
    monkeypatch.setenv("HPC_AGENT_WORKER_JSON_SCHEMA", "0")
    monkeypatch.setenv("HPC_AGENT_CODEX_OUTPUT_SCHEMA", "1")
    assert invoke_mod._worker_output_schema() is None
    assert invoke_mod._codex_output_schema() is not None


def test_codex_falls_back_to_stdout_when_no_last_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A worker that crashes before writing the last-message file leaves stdout
    # as the output so the caller's report-parse surfaces its last words.
    class _Proc:
        returncode = 1
        stdout = "Not authenticated. Goodbye."
        stderr = "boom"

    def _fake_run(argv: list[str], **kwargs: object) -> _Proc:
        return _Proc()  # never writes --output-last-message

    monkeypatch.setattr(invoke_mod.subprocess, "run", _fake_run)
    result = CodexCliInvoker().invoke(RenderedPrompt("P", "S"), cwd=tmp_path)
    assert result.output == "Not authenticated. Goodbye."
    assert result.exit_code == 1


def test_codex_missing_credential_remediation_none_when_key_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_API_KEY", "sk-codex-x")
    assert CodexCliInvoker().missing_credential_remediation() is None


def test_codex_missing_credential_remediation_message_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    msg = CodexCliInvoker().missing_credential_remediation()
    assert msg is not None
    assert "CODEX_API_KEY" in msg
    # Prefer CODEX_API_KEY over ambient OPENAI_API_KEY (shadow hazard, #3286).
    assert "OPENAI_API_KEY" in msg


def test_codex_scoped_key_authenticates_child_via_openai_api_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Codex authenticates from OPENAI_API_KEY (or a stored ChatGPT login), NOT
    # CODEX_API_KEY — which is only an hpc-agent-side name. The driver must map
    # the scoped CODEX_API_KEY onto the child's OPENAI_API_KEY, and it must WIN
    # over any ambient OPENAI_API_KEY / stored login the guard exists to bypass
    # (#3286). Otherwise the guard passes but Codex never sees the scoped key.
    monkeypatch.setenv("CODEX_API_KEY", "sk-codex-scoped")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-ambient-would-shadow")
    seen: dict[str, object] = {}
    monkeypatch.setattr(invoke_mod.subprocess, "run", _codex_capture_run(seen))
    CodexCliInvoker().invoke(RenderedPrompt("P", "S"), cwd=tmp_path)
    env = seen["env"]
    assert isinstance(env, dict)
    assert env["OPENAI_API_KEY"] == "sk-codex-scoped"


# ─── Gemini CLI invoker ─────────────────────────────────────────────────────


def _gemini_capture_run(seen: dict[str, object], *, response: str = "gemini report"):
    """A fake ``subprocess.run`` recording argv/cwd/env/stdin and the
    GEMINI_SYSTEM_MD content, returning the fixed {response, stats, error}
    envelope on stdout."""
    import json

    class _Proc:
        returncode = 0
        stdout = json.dumps({"response": response, "stats": {}, "error": None})
        stderr = ""

    def _fake_run(argv: list[str], **kwargs: object) -> _Proc:
        seen["argv"] = argv
        seen["cwd"] = kwargs.get("cwd")
        seen["env"] = kwargs.get("env")
        seen["input"] = kwargs.get("input")
        env = kwargs.get("env")
        assert isinstance(env, dict)
        seen["system_md"] = Path(env["GEMINI_SYSTEM_MD"]).read_text(encoding="utf-8")
        return _Proc()

    return _fake_run


def _redirect_gemini_policy_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point GEMINI_DIR at a temp dir so the policy TOML lands there, not ~/.gemini."""
    monkeypatch.setenv("GEMINI_DIR", str(tmp_path / "gemini-home"))
    return tmp_path / "gemini-home" / "policies"


def test_gemini_cli_invoker_builds_the_right_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _redirect_gemini_policy_dir(monkeypatch, tmp_path)
    seen: dict[str, object] = {}
    monkeypatch.setattr(invoke_mod.subprocess, "run", _gemini_capture_run(seen))

    prompt = RenderedPrompt(cacheable_prefix="PREFIX", variable_suffix="SUFFIX")
    result = GeminiCliInvoker().invoke(prompt, cwd=tmp_path)

    assert isinstance(result, InvocationResult)
    assert result.exit_code == 0
    # Output is the unwrapped `response` field of the JSON envelope.
    assert result.output == "gemini report"
    assert result.cache_stats is None

    argv = seen["argv"]
    assert isinstance(argv, list)
    assert argv[0:2] == ["gemini", "-p"]
    # Concrete model id pin (not an alias).
    assert argv[argv.index("--model") + 1] == "gemini-2.5-flash"
    assert "--output-format" in argv and "json" in argv
    # The cacheable prefix is the full-replacement system prompt; the variable
    # suffix is the user prompt on stdin. Both off argv (#169).
    assert seen["system_md"] == "PREFIX"
    assert seen["input"] == "SUFFIX"
    assert "PREFIX" not in argv and "SUFFIX" not in argv
    assert seen["cwd"] == str(tmp_path)


def test_gemini_does_not_select_a_sandbox_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # GEMINI_SANDBOX names a backend, not a bool — it must be unset so no
    # sandbox is selected (the worker SSH/rsyncs out).
    _redirect_gemini_policy_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("GEMINI_SANDBOX", "docker")
    seen: dict[str, object] = {}
    monkeypatch.setattr(invoke_mod.subprocess, "run", _gemini_capture_run(seen))
    GeminiCliInvoker().invoke(RenderedPrompt("P", "S"), cwd=tmp_path)
    env = seen["env"]
    assert isinstance(env, dict)
    assert "GEMINI_SANDBOX" not in env


def test_gemini_policy_toml_installed_at_user_tier_with_full_deny(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    policy_dir = _redirect_gemini_policy_dir(monkeypatch, tmp_path)
    seen: dict[str, object] = {}
    monkeypatch.setattr(invoke_mod.subprocess, "run", _gemini_capture_run(seen))
    GeminiCliInvoker().invoke(RenderedPrompt("P", "S"), cwd=tmp_path)

    # The fence lands at the USER tier (~/.gemini/policies via GEMINI_DIR), NOT
    # the workspace tier (which silently no-ops, #18186).
    toml_files = list(policy_dir.glob("*.toml"))
    assert len(toml_files) == 1
    toml = toml_files[0].read_text(encoding="utf-8")
    # Every cluster-op command is denied at a priority above any allow tier.
    for cmd in invoke_mod._CLUSTER_OP_DENY_COMMANDS:
        assert f'commandPrefix = "{cmd}"' in toml
    assert 'decision = "deny"' in toml
    assert f"priority = {invoke_mod._GEMINI_DENY_PRIORITY}" in toml
    for op in ("scancel", "qdel", "bkill", "ssh", "rsync", "scp", "curl", "wget"):
        assert op in toml


def test_gemini_model_pin_overridable_by_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _redirect_gemini_policy_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("HPC_AGENT_GEMINI_WORKER_MODEL", "gemini-2.5-custom")
    seen: dict[str, object] = {}
    monkeypatch.setattr(invoke_mod.subprocess, "run", _gemini_capture_run(seen))
    GeminiCliInvoker().invoke(RenderedPrompt("P", "S"), cwd=tmp_path)
    argv = seen["argv"]
    assert isinstance(argv, list)
    assert argv[argv.index("--model") + 1] == "gemini-2.5-custom"


def test_gemini_unwraps_response_envelope() -> None:
    import json

    envelope = json.dumps({"response": "the report", "stats": {"x": 1}, "error": None})
    assert invoke_mod._unwrap_gemini_json(envelope) == "the report"


def test_gemini_non_json_stdout_returned_verbatim() -> None:
    # A crash before the envelope leaves raw stdout so report-parse sees the
    # worker's last words (the #304 floor handles it).
    assert invoke_mod._unwrap_gemini_json("Not authenticated. Bye.") == "Not authenticated. Bye."


def test_gemini_missing_credential_remediation_none_when_gemini_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    assert GeminiCliInvoker().missing_credential_remediation() is None


def test_gemini_missing_credential_remediation_none_when_google_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "v-key")
    assert GeminiCliInvoker().missing_credential_remediation() is None


def test_gemini_missing_credential_remediation_message_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    msg = GeminiCliInvoker().missing_credential_remediation()
    assert msg is not None
    assert "GEMINI_API_KEY" in msg and "GOOGLE_API_KEY" in msg


# ─── registry + multi-harness auto-selection ────────────────────────────────


def test_codex_and_gemini_registered() -> None:
    assert get_invoker("codex-cli").name == "codex-cli"
    assert get_invoker("gemini-cli").name == "gemini-cli"


def test_env_override_selects_codex_and_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_AGENT_INVOKER", "codex-cli")
    assert get_invoker().name == "codex-cli"
    monkeypatch.setenv("HPC_AGENT_INVOKER", "gemini-cli")
    assert get_invoker().name == "gemini-cli"


def test_auto_select_falls_through_to_codex_when_only_codex_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_worker_credentials(monkeypatch)
    monkeypatch.setenv("CODEX_API_KEY", "sk-codex")
    assert get_invoker().name == "codex-cli"


def test_auto_select_falls_through_to_gemini_when_only_gemini_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_worker_credentials(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    assert get_invoker().name == "gemini-cli"


def test_auto_select_falls_through_to_gemini_when_only_google_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_worker_credentials(monkeypatch)
    monkeypatch.setenv("GOOGLE_API_KEY", "v-key")
    assert get_invoker().name == "gemini-cli"


def test_codex_outranks_gemini_when_both_present(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_worker_credentials(monkeypatch)
    monkeypatch.setenv("CODEX_API_KEY", "sk-codex")
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    assert get_invoker().name == "codex-cli"


def test_claude_creds_still_win_over_codex_and_gemini(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression: adding Codex/Gemini fall-through must NOT change Claude
    # selection. Claude creds present → claude-cli even when Codex/Gemini keys
    # are also set.
    _clear_worker_credentials(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setenv("CODEX_API_KEY", "sk-codex")
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    assert get_invoker().name == "claude-cli"


def test_claude_oauth_still_wins_over_codex_and_gemini(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No Claude API key, but a Claude OAuth creds file → claude-cli-oauth still
    # beats Codex/Gemini keys (OAuth tier is checked before the fall-through).
    _clear_worker_credentials(monkeypatch)
    monkeypatch.setattr(invoke_mod, "_oauth_credentials_available", lambda: True)
    monkeypatch.setenv("CODEX_API_KEY", "sk-codex")
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    assert get_invoker().name == "claude-cli-oauth"


def test_old_cli_json_schema_failure_names_the_off_switch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A `claude` CLI predating --json-schema rejects the flag; with the
    # constraint on by default the failure must carry its own remediation.
    class _Proc:
        returncode = 2
        stdout = ""
        stderr = "error: unknown option '--json-schema'"

    def _fake_run(argv, **kwargs):
        idx = argv.index("--append-system-prompt-file") + 1
        Path(argv[idx]).read_text(encoding="utf-8")
        return _Proc()

    monkeypatch.delenv("HPC_AGENT_WORKER_JSON_SCHEMA", raising=False)
    monkeypatch.setattr(invoke_mod.subprocess, "run", _fake_run)
    result = ClaudeCliInvoker().invoke(
        RenderedPrompt(cacheable_prefix="P", variable_suffix="S"), cwd=tmp_path
    )
    assert result.exit_code == 2
    assert "HPC_AGENT_WORKER_JSON_SCHEMA=0" in result.stderr
