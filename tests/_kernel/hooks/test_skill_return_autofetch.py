"""Tests for the ``skill_return_autofetch`` PostToolUse hook.

The hook is harness-mediated: Claude Code runs it after every matched tool
call, feeding the PostToolUse payload on stdin. It is a pure, additive,
fail-open observer — on the happy path (a known sub-skill's
``emit-skill-return`` Bash call just committed its envelope) it injects the
envelope as ``additionalContext``; for everything else it is a clean no-op.

The trigger is the ``Bash`` call that runs ``emit-skill-return`` — NOT the
``Skill`` tool. Claude Code's Skill tool returns immediately (its result is
the injected instructions), *before* the sub-skill body runs, so a
Skill-matched hook can never observe a fresh envelope (pre-0.10.58 bug: the
hook was a structural no-op on every fresh run). These tests pin both the
pure core (:func:`build_hook_output`) and the stdin/stdout ``main`` wrapper,
including every defensive no-op branch (non-Bash tool, non-emit command,
unknown skill, missing file, malformed envelope JSON, malformed payload).
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from hpc_agent._kernel.hooks import skill_return_autofetch as hook
from hpc_agent.cli.skill_returns import _KNOWN_SKILLS, _committed_path

_KNOWN_SKILL = "hpc-aggregate"
_SAMPLE_ENVELOPE = {
    "ok": True,
    "skill": _KNOWN_SKILL,
    "run_id": "forecast-2026-01-01-abcd",
    "profile": "forecast",
    "stage": "final",
}


def _commit(exp: Path, skill: str, envelope: dict) -> Path:
    """Write a committed return envelope at the canonical path."""
    committed = _committed_path(exp, skill)
    committed.parent.mkdir(parents=True, exist_ok=True)
    committed.write_text(json.dumps(envelope), encoding="utf-8")
    return committed


def _emit_command(skill: str, exp: Path | None = None) -> str:
    cmd = f"hpc-agent emit-skill-return --skill {skill}"
    if exp is not None:
        cmd += f" --experiment-dir {exp.as_posix()}"
    return cmd


def _payload(exp: Path, skill: str, *, tool_name: str = "Bash", command: str | None = None) -> dict:
    """A minimal PostToolUse payload for an ``emit-skill-return`` Bash call."""
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": tool_name,
        "tool_input": {"command": command if command is not None else _emit_command(skill, exp)},
        "tool_response": {},
        "cwd": str(exp),
    }


# ─── happy path: emit for a known skill → reads & injects ───────────────────


def test_emit_for_known_skill_injects_envelope(tmp_path: Path) -> None:
    _commit(tmp_path, _KNOWN_SKILL, _SAMPLE_ENVELOPE)

    out = hook.build_hook_output(_payload(tmp_path, _KNOWN_SKILL))

    assert out is not None
    spec = out["hookSpecificOutput"]
    assert spec["hookEventName"] == "PostToolUse"
    # The injected context is the verbatim envelope, canonicalised the same way
    # fetch-skill-return prints it (sort_keys=True).
    assert json.loads(spec["additionalContext"]) == _SAMPLE_ENVELOPE
    assert spec["additionalContext"] == json.dumps(_SAMPLE_ENVELOPE, sort_keys=True)


def test_injection_does_not_delete_the_return_file(tmp_path: Path) -> None:
    """The hook is additive — it must NOT clear the file (unlike fetch)."""
    committed = _commit(tmp_path, _KNOWN_SKILL, _SAMPLE_ENVELOPE)

    hook.build_hook_output(_payload(tmp_path, _KNOWN_SKILL))

    assert committed.exists(), "hook must leave the file for the manual fetch seam"


@pytest.mark.parametrize("skill", list(_KNOWN_SKILLS))
def test_every_known_skill_resolves(tmp_path: Path, skill: str) -> None:
    _commit(tmp_path, skill, {"ok": True, "skill": skill})
    out = hook.build_hook_output(_payload(tmp_path, skill))
    assert out is not None
    assert json.loads(out["hookSpecificOutput"]["additionalContext"])["skill"] == skill


def test_experiment_dir_flag_wins_over_cwd(tmp_path: Path) -> None:
    """``--experiment-dir`` names the dir the emitter wrote to — it must win."""
    exp = tmp_path / "exp"
    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()
    _commit(exp, _KNOWN_SKILL, _SAMPLE_ENVELOPE)

    payload = _payload(exp, _KNOWN_SKILL)
    payload["cwd"] = str(other_cwd)  # cwd points away; the flag must still hit
    assert hook.build_hook_output(payload) is not None


def test_command_without_experiment_dir_falls_back_to_cwd(tmp_path: Path) -> None:
    _commit(tmp_path, _KNOWN_SKILL, _SAMPLE_ENVELOPE)
    payload = _payload(tmp_path, _KNOWN_SKILL, command=_emit_command(_KNOWN_SKILL))
    assert hook.build_hook_output(payload) is not None


def test_chained_command_still_resolves(tmp_path: Path) -> None:
    """The emit chained with `&&` (the chaining discipline) must still fire."""
    _commit(tmp_path, _KNOWN_SKILL, _SAMPLE_ENVELOPE)
    command = f"{_emit_command(_KNOWN_SKILL, tmp_path)} && echo done"
    payload = _payload(tmp_path, _KNOWN_SKILL, command=command)
    assert hook.build_hook_output(payload) is not None


def test_flag_equals_form_resolves(tmp_path: Path) -> None:
    _commit(tmp_path, _KNOWN_SKILL, _SAMPLE_ENVELOPE)
    command = (
        f"hpc-agent emit-skill-return --skill={_KNOWN_SKILL} --experiment-dir={tmp_path.as_posix()}"
    )
    payload = _payload(tmp_path, _KNOWN_SKILL, command=command)
    assert hook.build_hook_output(payload) is not None


def test_quoted_experiment_dir_resolves(tmp_path: Path) -> None:
    """A double-quoted --experiment-dir (spaces in path) resolves."""
    exp = tmp_path / "dir with spaces"
    _commit(exp, _KNOWN_SKILL, _SAMPLE_ENVELOPE)
    command = (
        f'hpc-agent emit-skill-return --skill {_KNOWN_SKILL} --experiment-dir "{exp.as_posix()}"'
    )
    payload = _payload(exp, _KNOWN_SKILL, command=command)
    payload["cwd"] = str(tmp_path)  # cwd would miss; the quoted flag must hit
    assert hook.build_hook_output(payload) is not None


# ─── defensive no-ops ───────────────────────────────────────────────────────


def test_non_bash_tool_is_noop(tmp_path: Path) -> None:
    _commit(tmp_path, _KNOWN_SKILL, _SAMPLE_ENVELOPE)
    payload = _payload(tmp_path, _KNOWN_SKILL, tool_name="Skill")
    assert hook.build_hook_output(payload) is None


def test_skill_tool_invocation_is_noop(tmp_path: Path) -> None:
    """The pre-0.10.58 trigger shape (Skill tool, skill name in command) is dead.

    At PostToolUse(Skill) time the sub-skill body has not run — any envelope
    on disk is stale by construction, so injecting it would be harmful.
    """
    _commit(tmp_path, _KNOWN_SKILL, _SAMPLE_ENVELOPE)
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Skill",
        "tool_input": {"command": _KNOWN_SKILL},
        "tool_response": {},
        "cwd": str(tmp_path),
    }
    assert hook.build_hook_output(payload) is None


def test_non_emit_bash_command_is_noop(tmp_path: Path) -> None:
    _commit(tmp_path, _KNOWN_SKILL, _SAMPLE_ENVELOPE)
    payload = _payload(tmp_path, _KNOWN_SKILL, command="hpc-agent discover")
    assert hook.build_hook_output(payload) is None


def test_unknown_skill_is_noop(tmp_path: Path) -> None:
    # File present, but the emitted skill is not registered → no injection.
    _commit(tmp_path, "hpc-aggregate", _SAMPLE_ENVELOPE)
    payload = _payload(
        tmp_path,
        _KNOWN_SKILL,
        command="hpc-agent emit-skill-return --skill totally-unknown-skill",
    )
    assert hook.build_hook_output(payload) is None


def test_emit_without_skill_flag_is_noop(tmp_path: Path) -> None:
    _commit(tmp_path, _KNOWN_SKILL, _SAMPLE_ENVELOPE)
    payload = _payload(tmp_path, _KNOWN_SKILL, command="hpc-agent emit-skill-return --help")
    assert hook.build_hook_output(payload) is None


def test_missing_return_file_is_noop(tmp_path: Path) -> None:
    # Known skill, well-formed payload, but no committed envelope on disk
    # (e.g. the emit itself failed schema validation).
    assert hook.build_hook_output(_payload(tmp_path, _KNOWN_SKILL)) is None


def test_malformed_envelope_json_is_noop(tmp_path: Path) -> None:
    committed = _committed_path(tmp_path, _KNOWN_SKILL)
    committed.parent.mkdir(parents=True, exist_ok=True)
    committed.write_text("{not valid json", encoding="utf-8")
    assert hook.build_hook_output(_payload(tmp_path, _KNOWN_SKILL)) is None


def test_malformed_payload_is_noop() -> None:
    for bad in (None, [], "string", 42, {"tool_name": "Bash"}):
        assert hook.build_hook_output(bad) is None


def test_missing_tool_input_is_noop(tmp_path: Path) -> None:
    payload = {"tool_name": "Bash", "cwd": str(tmp_path)}
    assert hook.build_hook_output(payload) is None


def test_non_string_command_is_noop(tmp_path: Path) -> None:
    payload = _payload(tmp_path, _KNOWN_SKILL)
    payload["tool_input"] = {"command": 7}
    assert hook.build_hook_output(payload) is None


def test_absent_cwd_falls_back_to_process_cwd(tmp_path: Path, monkeypatch) -> None:
    """No ``cwd`` in payload, no flag → resolve against the process cwd."""
    _commit(tmp_path, _KNOWN_SKILL, _SAMPLE_ENVELOPE)
    monkeypatch.chdir(tmp_path)
    payload = _payload(tmp_path, _KNOWN_SKILL, command=_emit_command(_KNOWN_SKILL))
    del payload["cwd"]
    out = hook.build_hook_output(payload)
    assert out is not None


# ─── extract_emit_invocation unit ───────────────────────────────────────────


def test_extract_emit_invocation_variants() -> None:
    extract = hook.extract_emit_invocation
    assert extract("hpc-agent emit-skill-return --skill hpc-aggregate") == (
        "hpc-aggregate",
        None,
    )
    assert extract(
        "hpc-agent emit-skill-return --skill hpc-status --experiment-dir C:/Users/x/demo"
    ) == ("hpc-status", "C:/Users/x/demo")
    assert extract(
        "hpc-agent emit-skill-return --skill=hpc-status --experiment-dir='/tmp/a b'"
    ) == ("hpc-status", "/tmp/a b")
    assert extract(
        'hpc-agent emit-skill-return --skill "hpc-status" --experiment-dir "/tmp/a b"'
    ) == ("hpc-status", "/tmp/a b")
    # Chained command: the bare dir token stops at the shell metacharacter.
    assert extract(
        "hpc-agent emit-skill-return --skill hpc-status --experiment-dir /tmp/x && echo ok"
    ) == ("hpc-status", "/tmp/x")
    assert extract("hpc-agent fetch-skill-return --skill hpc-status") is None
    assert extract("hpc-agent emit-skill-return --no-such-flag") is None
    assert extract("echo hello") is None
    assert extract(7) is None
    assert extract(None) is None


# ─── main() stdin/stdout wrapper ────────────────────────────────────────────


def _run_main(monkeypatch, stdin_text: str) -> tuple[int, str]:
    """Invoke ``main`` with *stdin_text* on stdin; capture (rc, stdout)."""
    out_buf = io.StringIO()
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
    monkeypatch.setattr("sys.stdout", out_buf)
    rc = hook.main([])
    return rc, out_buf.getvalue()


def test_main_known_skill_prints_hook_output(tmp_path: Path, monkeypatch) -> None:
    _commit(tmp_path, _KNOWN_SKILL, _SAMPLE_ENVELOPE)
    rc, out = _run_main(monkeypatch, json.dumps(_payload(tmp_path, _KNOWN_SKILL)))
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert json.loads(parsed["hookSpecificOutput"]["additionalContext"]) == _SAMPLE_ENVELOPE


def test_main_noop_prints_nothing(tmp_path: Path, monkeypatch) -> None:
    """Non-emit Bash command → main exits 0 and emits no stdout (clean no-op)."""
    payload = _payload(tmp_path, _KNOWN_SKILL, command="git status")
    rc, out = _run_main(monkeypatch, json.dumps(payload))
    assert rc == 0
    assert out == ""


def test_main_malformed_stdin_is_clean_noop(monkeypatch) -> None:
    rc, out = _run_main(monkeypatch, "{not json")
    assert rc == 0
    assert out == ""


def test_main_empty_stdin_is_clean_noop(monkeypatch) -> None:
    rc, out = _run_main(monkeypatch, "")
    assert rc == 0
    assert out == ""


def test_main_never_raises_on_core_error(tmp_path: Path, monkeypatch) -> None:
    """A bug inside the core degrades to a no-op, never a non-zero exit."""

    def _boom(_payload):
        raise RuntimeError("simulated core failure")

    monkeypatch.setattr(hook, "build_hook_output", _boom)
    rc, out = _run_main(monkeypatch, json.dumps(_payload(tmp_path, _KNOWN_SKILL)))
    assert rc == 0
    assert out == ""
