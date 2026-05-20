"""Tests for the /monitor-hpc Stop-hook enforcement script + installer."""

from __future__ import annotations

import io
import json
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent.hooks import install as install_mod
from hpc_agent.hooks import monitor_armed_check as hook

if TYPE_CHECKING:
    from pathlib import Path

# ─── transcript fixtures ────────────────────────────────────────────────────


def _write_transcript(path: Path, messages: list[dict[str, Any]]) -> None:
    path.write_text("\n".join(json.dumps(m) for m in messages) + "\n", encoding="utf-8")


def _payload(transcript_path: Path, *, output: str = "") -> dict[str, Any]:
    return {
        "session_id": "s_test",
        "transcript_path": str(transcript_path),
        "cwd": "/tmp",
        "permission_mode": "default",
        "hook_event_name": "Stop",
        "stop_reason": "end_turn",
        "output": output,
        "tool_uses": [],
    }


# ─── hook script ────────────────────────────────────────────────────────────


def test_hook_allows_when_not_a_monitor_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    transcript = tmp_path / "session.jsonl"
    _write_transcript(
        transcript,
        [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ],
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_payload(transcript))))
    rc = hook.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert out == ""  # no decision emitted -> allow stop


def test_hook_allows_when_monitor_armed_line_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    transcript = tmp_path / "session.jsonl"
    _write_transcript(
        transcript,
        [
            {"role": "user", "content": "/monitor-hpc ml_ridge"},
            {
                "role": "assistant",
                "content": (
                    "all 32 tasks running; rescheduled.\n"
                    'armed: cron run_id=ml_ridge-2026 cadence=300s reason="queue wait"'
                ),
            },
        ],
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_payload(transcript))))
    assert hook.main() == 0
    assert capsys.readouterr().out == ""


def test_hook_blocks_when_armed_line_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    transcript = tmp_path / "session.jsonl"
    _write_transcript(
        transcript,
        [
            {"role": "user", "content": "/monitor-hpc ml_ridge"},
            {"role": "assistant", "content": "all good, see you next time"},
        ],
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_payload(transcript))))
    assert hook.main() == 0
    out = capsys.readouterr().out
    decision = json.loads(out)
    assert decision["decision"] == "block"
    assert "armed:" in decision["reason"]
    assert "monitor-hpc" in decision["reason"]


def test_hook_accepts_inline_output_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When Claude Code passes the assistant text inline, the hook uses it."""
    transcript = tmp_path / "session.jsonl"
    _write_transcript(
        transcript,
        [{"role": "user", "content": "/monitor-hpc"}],
    )
    payload = _payload(
        transcript,
        output='armed: cron run_id=x cadence=60s reason="warm"',
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert hook.main() == 0
    assert capsys.readouterr().out == ""


def test_hook_handles_anthropic_content_block_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    transcript = tmp_path / "session.jsonl"
    _write_transcript(
        transcript,
        [
            {
                "role": "user",
                "content": [{"type": "text", "text": "/monitor-hpc ml_ridge"}],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "tick complete."},
                    {
                        "type": "text",
                        "text": 'armed: loop run_id=ml_ridge cadence=300s reason="ok"',
                    },
                ],
            },
        ],
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_payload(transcript))))
    assert hook.main() == 0
    assert capsys.readouterr().out == ""


def test_hook_rejects_wakeup_mechanism(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ScheduleWakeup is off-spec. ``armed: wakeup ...`` must still fail."""
    transcript = tmp_path / "session.jsonl"
    _write_transcript(
        transcript,
        [
            {"role": "user", "content": "/monitor-hpc"},
            {
                "role": "assistant",
                "content": 'armed: wakeup run_id=x cadence=60s reason="bad"',
            },
        ],
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_payload(transcript))))
    assert hook.main() == 0
    decision = json.loads(capsys.readouterr().out)
    assert decision["decision"] == "block"


def test_hook_tolerates_missing_transcript(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = {
        "session_id": "s",
        "transcript_path": "/no/such/file",
        "hook_event_name": "Stop",
        "stop_reason": "end_turn",
        "output": "",
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    # No user text at all -> not a /monitor-hpc turn -> allow
    assert hook.main() == 0
    assert capsys.readouterr().out == ""


def test_hook_tolerates_malformed_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("{not valid"))
    assert hook.main() == 0
    assert capsys.readouterr().out == ""


# ─── installer ──────────────────────────────────────────────────────────────


def _commands(entry: dict[str, Any]) -> list[str]:
    """Command strings in a ``hooks.Stop`` entry — group shape or legacy flat."""
    if "hooks" in entry:
        return [h["command"] for h in entry["hooks"]]
    return [entry["command"]]


def test_install_creates_settings_when_missing(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    summary = install_mod.install_hooks(settings_path=settings)
    assert summary["wrote"] is True
    assert summary["added"] == ["monitor-armed"]
    on_disk = json.loads(settings.read_text())
    assert "Stop" in on_disk["hooks"]
    entry = on_disk["hooks"]["Stop"][0]
    # Each Stop element must be a group object wrapping a `hooks` array;
    # a bare command hook here is rejected by Claude Code.
    assert "hooks" in entry
    assert "hpc_agent.hooks.monitor_armed_check" in entry["hooks"][0]["command"]


def test_install_is_idempotent(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    install_mod.install_hooks(settings_path=settings)
    summary = install_mod.install_hooks(settings_path=settings)
    assert summary["wrote"] is False
    assert summary["added"] == []
    assert summary["already_installed"] == ["monitor-armed"]


def test_install_preserves_existing_hooks(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [{"type": "command", "command": "echo unrelated"}],
                    "PreToolUse": [{"type": "command", "command": "echo other"}],
                }
            }
        )
    )
    install_mod.install_hooks(settings_path=settings)
    on_disk = json.loads(settings.read_text())
    stop_entries = on_disk["hooks"]["Stop"]
    assert len(stop_entries) == 2
    all_cmds = [c for e in stop_entries for c in _commands(e)]
    assert any("echo unrelated" in c for c in all_cmds)
    assert any("monitor_armed_check" in c for c in all_cmds)
    assert "PreToolUse" in on_disk["hooks"]


def test_install_heals_legacy_flat_entry(tmp_path: Path) -> None:
    """A flat command hook from the old buggy installer is rewritten in place.

    Pre-fix, ``settings_entry`` returned a bare ``{"type": "command",
    ...}`` hook and the installer appended it straight into ``hooks.Stop``
    — a shape Claude Code rejects. Re-running the fixed installer must
    heal that entry into the group shape, not duplicate it.
    """
    settings = tmp_path / "settings.json"
    command = "python -m hpc_agent.hooks.monitor_armed_check"
    settings.write_text(json.dumps({"hooks": {"Stop": [{"type": "command", "command": command}]}}))
    summary = install_mod.install_hooks(settings_path=settings)
    assert summary["wrote"] is True
    assert summary["added"] == ["monitor-armed"]
    stop_entries = json.loads(settings.read_text())["hooks"]["Stop"]
    # Healed in place — exactly one entry, now in the group shape.
    assert len(stop_entries) == 1
    assert "hooks" in stop_entries[0]
    assert _commands(stop_entries[0]) == [command]
    # And healing is idempotent.
    again = install_mod.install_hooks(settings_path=settings)
    assert again["wrote"] is False
    assert again["added"] == []


def test_install_preserves_other_top_level_keys(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"theme": "dark", "model": "sonnet"}))
    install_mod.install_hooks(settings_path=settings)
    on_disk = json.loads(settings.read_text())
    assert on_disk["theme"] == "dark"
    assert on_disk["model"] == "sonnet"
    assert "hooks" in on_disk


def test_install_dry_run_does_not_write(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    summary = install_mod.install_hooks(settings_path=settings, dry_run=True)
    assert summary["wrote"] is False
    assert summary["added"] == ["monitor-armed"]
    assert not settings.exists()


def test_install_rejects_bad_hooks_shape(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": ["not", "an", "object"]}))
    with pytest.raises(ValueError, match="hooks block"):
        install_mod.install_hooks(settings_path=settings)
