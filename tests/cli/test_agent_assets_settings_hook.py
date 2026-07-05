"""Tests for the settings.json merge that wires the skill-return hooks.

``install-commands`` / ``setup`` injects two hooks into
``<claude_dir>/settings.json``: the ``PostToolUse`` autofetch (matcher
``Bash``, with a bash-level ``emit-skill-return`` pre-filter so the every-
Bash-call common path never pays a Python interpreter start) and the ``Stop``
guard that blocks ending the turn over an unfetched return envelope. The
injection MUST be:

* **additive** — never clobber existing hooks, permissions, or any other key;
* **idempotent** — re-running install does not append a duplicate entry;
* **self-healing** — a stale prior entry (moved venv, pre-0.10.10 broken path
  encoding, pre-0.10.58 ``matcher: "Skill"`` shape) is replaced in place;
* **safe** — an existing-but-unparseable settings.json is left untouched.

These pin every branch of :func:`hpc_agent.agent_assets._merge_hook_entry`
through the public :func:`install_agent_assets` entrypoint, plus a dry-run
that writes nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

from hpc_agent.agent_assets import install_agent_assets

_HOOK_MODULE = "hpc_agent._kernel.hooks.skill_return_autofetch"
_STOP_MODULE = "hpc_agent._kernel.hooks.skill_return_stop_guard"


def _settings(claude_dir: Path) -> dict:
    loaded = json.loads((claude_dir / "settings.json").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _post_tool_use(settings: dict) -> list:
    entries = settings["hooks"]["PostToolUse"]
    assert isinstance(entries, list)
    return entries


def _entries_with_module(entries: list, module: str) -> list:
    out = []
    for entry in entries:
        for h in entry.get("hooks", []):
            if module in h.get("command", ""):
                out.append(entry)
    return out


def _autofetch_entries(settings: dict) -> list:
    return _entries_with_module(_post_tool_use(settings), _HOOK_MODULE)


def _stop_entries(settings: dict) -> list:
    return _entries_with_module(settings["hooks"].get("Stop", []), _STOP_MODULE)


# ─── fresh install: no settings.json yet ────────────────────────────────────


def test_creates_settings_json_when_absent(tmp_path: Path) -> None:
    result = install_agent_assets(claude_dir=tmp_path)

    assert result["settings_hook"]["action"] == "added"
    assert result["settings_hook"]["wrote"] is True
    assert result["settings_stop_hook"]["action"] == "added"
    assert result["settings_stop_hook"]["wrote"] is True

    settings = _settings(tmp_path)
    entries = _autofetch_entries(settings)
    assert len(entries) == 1
    # PostToolUse(Skill) fires when the Skill tool returns its instructions —
    # BEFORE the sub-skill body runs — so the autofetch must match Bash (the
    # emit-skill-return call) to ever see a fresh envelope.
    assert entries[0]["matcher"] == "Bash"
    # The bash pre-filter keeps non-emit Bash calls at builtin cost.
    assert "emit-skill-return" in entries[0]["hooks"][0]["command"]

    stop_entries = _stop_entries(settings)
    assert len(stop_entries) == 1
    assert "matcher" not in stop_entries[0]  # Stop has no tool to match


# ─── idempotency ────────────────────────────────────────────────────────────


def test_rerun_does_not_duplicate_either_entry(tmp_path: Path) -> None:
    first = install_agent_assets(claude_dir=tmp_path)
    assert first["settings_hook"]["action"] == "added"
    assert first["settings_stop_hook"]["action"] == "added"

    second = install_agent_assets(claude_dir=tmp_path)
    assert second["settings_hook"]["action"] == "already-present"
    assert second["settings_hook"]["wrote"] is False
    assert second["settings_stop_hook"]["action"] == "already-present"
    assert second["settings_stop_hook"]["wrote"] is False

    settings = _settings(tmp_path)
    assert len(_autofetch_entries(settings)) == 1
    assert len(_stop_entries(settings)) == 1


def test_stale_hook_command_is_replaced_in_place(tmp_path: Path) -> None:
    """A stale entry (different interpreter path, or pre-0.10.10 broken encoding) is updated.

    Earlier versions emitted a raw ``sys.executable`` Windows backslash path
    that bash mis-interpreted as escape sequences ('C:\\\\U' → 'C:U'), and the
    old idempotency check (module-path-only) treated the broken entry as
    already-present, so ``install-commands`` could not heal it. The merge
    matches on module path AND replaces a mismatched command, so a re-run from
    a fresh install repoints the entry without appending a duplicate.
    """
    settings_path = tmp_path / "settings.json"
    stale_command = f"/some/other/venv/bin/python -m {_HOOK_MODULE}"
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": stale_command,
                                }
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    result = install_agent_assets(claude_dir=tmp_path)
    assert result["settings_hook"]["action"] == "updated"
    assert result["settings_hook"]["wrote"] is True

    entries = _autofetch_entries(_settings(tmp_path))
    assert len(entries) == 1
    # The stale command is gone — replaced with the canonical install-time form.
    assert entries[0]["hooks"][0]["command"] != stale_command


def test_pre_0_10_58_skill_matcher_entry_is_migrated(tmp_path: Path) -> None:
    """The pre-0.10.58 ``matcher: "Skill"`` shape is healed to ``Bash`` in place.

    The old matcher fired at Skill-tool return — before the sub-skill body ran
    — so the hook was a structural no-op (2026-06-10 demo: the envelope sat
    unfetched and the turn ended). install-commands must repoint the existing
    entry, not append a second one beside the dead one.
    """
    settings_path = tmp_path / "settings.json"
    old_entry = {
        "matcher": "Skill",
        "hooks": [
            {
                "type": "command",
                "command": f"C:/Users/x/.venv/Scripts/python.exe -m {_HOOK_MODULE}",
            }
        ],
    }
    settings_path.write_text(json.dumps({"hooks": {"PostToolUse": [old_entry]}}), encoding="utf-8")

    result = install_agent_assets(claude_dir=tmp_path)
    assert result["settings_hook"]["action"] == "updated"

    entries = _autofetch_entries(_settings(tmp_path))
    assert len(entries) == 1
    assert entries[0]["matcher"] == "Bash"


def test_byte_equal_entry_is_already_present(tmp_path: Path) -> None:
    """An entry byte-equal to the canonical install short-circuits as already-present."""
    # Bootstrap by running once — the first install writes the canonical entries.
    install_agent_assets(claude_dir=tmp_path)

    result = install_agent_assets(claude_dir=tmp_path)
    assert result["settings_hook"]["action"] == "already-present"
    assert result["settings_hook"]["wrote"] is False
    assert len(_autofetch_entries(_settings(tmp_path))) == 1


# ─── additive: preserve existing settings ───────────────────────────────────


def test_preserves_existing_settings_and_hooks(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    existing = {
        "permissions": {"deny": ["Bash(rm -rf:*)"]},
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "Edit|Write",
                    "hooks": [{"type": "command", "command": "ruff check"}],
                }
            ],
            "PreToolUse": [{"matcher": "Bash", "hooks": []}],
            "Stop": [{"hooks": [{"type": "command", "command": "echo bye"}]}],
        },
        "customKey": {"nested": [1, 2, 3]},
    }
    settings_path.write_text(json.dumps(existing), encoding="utf-8")

    install_agent_assets(claude_dir=tmp_path)
    settings = _settings(tmp_path)

    # Unrelated keys preserved verbatim. ``permissions.deny`` is untouched;
    # ``permissions.allow`` is augmented with Skill(<name>) entries by the
    # sibling _merge_skill_permissions step (see the dedicated test module
    # test_agent_assets_settings_permissions.py for the permissions contract).
    assert settings["permissions"]["deny"] == ["Bash(rm -rf:*)"]
    assert settings["customKey"] == {"nested": [1, 2, 3]}
    # The pre-existing PreToolUse entry survives; the scheduler write-fence
    # (conduct rule 7) is appended after it.
    ptu_pre = settings["hooks"]["PreToolUse"]
    assert ptu_pre[0] == {"matcher": "Bash", "hooks": []}
    assert len(ptu_pre) == 2
    assert "scheduler_write_fence" in ptu_pre[1]["hooks"][0]["command"]

    # The pre-existing PostToolUse entry survives, and ours is appended after it.
    ptu = _post_tool_use(settings)
    assert ptu[0]["matcher"] == "Edit|Write"
    assert ptu[0]["hooks"][0]["command"] == "ruff check"
    assert len(_autofetch_entries(settings)) == 1

    # The pre-existing Stop entry survives, and the guard is appended after it.
    stop = settings["hooks"]["Stop"]
    assert stop[0]["hooks"][0]["command"] == "echo bye"
    assert len(_stop_entries(settings)) == 1


def test_creates_event_arrays_when_hooks_exist_without_them(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"hooks": {"PreToolUse": []}}), encoding="utf-8")

    install_agent_assets(claude_dir=tmp_path)
    settings = _settings(tmp_path)

    # The empty PreToolUse array gains exactly the write-fence entry.
    ptu = settings["hooks"]["PreToolUse"]
    assert len(ptu) == 1
    assert "scheduler_write_fence" in ptu[0]["hooks"][0]["command"]
    assert len(_autofetch_entries(settings)) == 1
    assert len(_stop_entries(settings)) == 1


# ─── safety: refuse to clobber unparseable settings ─────────────────────────


def test_unparseable_settings_is_not_clobbered(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{ this is not valid json", encoding="utf-8")

    result = install_agent_assets(claude_dir=tmp_path)

    assert result["settings_hook"]["action"] == "skipped-unparseable"
    assert result["settings_hook"]["wrote"] is False
    assert result["settings_stop_hook"]["action"] == "skipped-unparseable"
    # The original (invalid) content is preserved untouched.
    assert settings_path.read_text(encoding="utf-8") == "{ this is not valid json"


def test_non_object_settings_is_not_clobbered(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    result = install_agent_assets(claude_dir=tmp_path)

    assert result["settings_hook"]["action"] == "skipped-unparseable"
    assert result["settings_stop_hook"]["action"] == "skipped-unparseable"
    assert json.loads(settings_path.read_text(encoding="utf-8")) == [1, 2, 3]


# ─── dry-run writes nothing ─────────────────────────────────────────────────


def test_dry_run_does_not_write_settings(tmp_path: Path) -> None:
    result = install_agent_assets(claude_dir=tmp_path, dry_run=True)

    assert result["settings_hook"]["action"] == "dry-run-would-add"
    assert result["settings_hook"]["wrote"] is False
    assert result["settings_stop_hook"]["action"] == "dry-run-would-add"
    assert result["settings_stop_hook"]["wrote"] is False
    assert not (tmp_path / "settings.json").exists()


# ─── SessionStart alert-count hook (proving run #3: alert delivery) ─────────


def test_session_start_alert_count_hook_is_wired(tmp_path: Path) -> None:
    result = install_agent_assets(claude_dir=tmp_path)
    assert result["settings_alert_count_hook"]["action"] == "added"
    assert result["settings_alert_count_hook"]["wrote"] is True

    settings = _settings(tmp_path)
    entries = _entries_with_module(
        settings["hooks"].get("SessionStart", []),
        "hpc_agent._kernel.hooks.alert_count",
    )
    assert len(entries) == 1
    assert "matcher" not in entries[0]  # SessionStart has no tool to match

    # Idempotent on re-run.
    second = install_agent_assets(claude_dir=tmp_path)
    assert second["settings_alert_count_hook"]["action"] == "already-present"
    settings = _settings(tmp_path)
    assert len(settings["hooks"]["SessionStart"]) == 1


# ─── UserPromptSubmit utterance capture (proving run #4: authorship lock) ───


def test_user_prompt_submit_utterance_hook_is_wired(tmp_path: Path) -> None:
    result = install_agent_assets(claude_dir=tmp_path)
    assert result["settings_utterance_hook"]["action"] == "added"
    assert result["settings_utterance_hook"]["wrote"] is True

    settings = _settings(tmp_path)
    entries = _entries_with_module(
        settings["hooks"].get("UserPromptSubmit", []),
        "hpc_agent._kernel.hooks.utterance_capture",
    )
    assert len(entries) == 1
    assert "matcher" not in entries[0]  # UserPromptSubmit has no tool to match

    # Idempotent on re-run.
    second = install_agent_assets(claude_dir=tmp_path)
    assert second["settings_utterance_hook"]["action"] == "already-present"
    settings = _settings(tmp_path)
    assert len(settings["hooks"]["UserPromptSubmit"]) == 1


# ─── PostToolUse answer capture (proving run #5: typed selector answers) ────


def test_ask_user_question_answer_capture_hook_is_wired(tmp_path: Path) -> None:
    result = install_agent_assets(claude_dir=tmp_path)
    assert result["settings_answer_capture_hook"]["action"] == "added"
    assert result["settings_answer_capture_hook"]["wrote"] is True

    settings = _settings(tmp_path)
    entries = _entries_with_module(
        _post_tool_use(settings),
        "hpc_agent._kernel.hooks.answer_capture",
    )
    assert len(entries) == 1
    assert entries[0]["matcher"] == "AskUserQuestion"

    # Idempotent on re-run, and the sibling PostToolUse entries are untouched.
    second = install_agent_assets(claude_dir=tmp_path)
    assert second["settings_answer_capture_hook"]["action"] == "already-present"
    settings = _settings(tmp_path)
    post = _post_tool_use(settings)
    assert len(_entries_with_module(post, "hpc_agent._kernel.hooks.answer_capture")) == 1
    assert len(_entries_with_module(post, _HOOK_MODULE)) == 1


# ─── Stop relay audit (conduct rule 10 staged → active) ─────────────────────


def test_stop_relay_audit_hook_is_wired(tmp_path: Path) -> None:
    result = install_agent_assets(claude_dir=tmp_path)
    assert result["settings_relay_audit_hook"]["action"] == "added"
    assert result["settings_relay_audit_hook"]["wrote"] is True

    settings = _settings(tmp_path)
    entries = _entries_with_module(
        settings["hooks"].get("Stop", []),
        "hpc_agent._kernel.hooks.relay_audit_stop",
    )
    assert len(entries) == 1
    assert "matcher" not in entries[0]  # Stop has no tool to match

    # Idempotent on re-run, and the sibling Stop guards are untouched.
    second = install_agent_assets(claude_dir=tmp_path)
    assert second["settings_relay_audit_hook"]["action"] == "already-present"
    settings = _settings(tmp_path)
    stop_entries = settings["hooks"]["Stop"]
    assert len(_entries_with_module(stop_entries, "hpc_agent._kernel.hooks.relay_audit_stop")) == 1
    assert len(_entries_with_module(stop_entries, _STOP_MODULE)) == 1
