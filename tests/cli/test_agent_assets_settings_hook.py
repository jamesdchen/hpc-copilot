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
# The three legacy standalone Stop guards are fused into ONE ``stop_multiplex``
# entry (#288). The installed Stop command names the multiplex module AND the
# three guard modules (as args), so the fused entry's command mentions all four.
_STOP_MODULE = "hpc_agent._kernel.hooks.stop_multiplex"
_SKILL_RETURN_STOP_MODULE = "hpc_agent._kernel.hooks.skill_return_stop_guard"
_RENDEZVOUS_STOP_MODULE = "hpc_agent._kernel.hooks.decision_rendezvous_stop_guard"
_RELAY_AUDIT_MODULE = "hpc_agent._kernel.hooks.relay_audit_stop"


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
    assert result["settings_stop_multiplex_hook"]["action"] == "added"
    assert result["settings_stop_multiplex_hook"]["wrote"] is True

    settings = _settings(tmp_path)
    entries = _autofetch_entries(settings)
    assert len(entries) == 1
    # PostToolUse(Skill) fires when the Skill tool returns its instructions —
    # BEFORE the sub-skill body runs — so the autofetch must match Bash (the
    # emit-skill-return call) to ever see a fresh envelope.
    assert entries[0]["matcher"] == "Bash"
    # The bash pre-filter keeps non-emit Bash calls at builtin cost.
    assert "emit-skill-return" in entries[0]["hooks"][0]["command"]

    # Exactly ONE fused Stop entry, and its command names each of the three
    # guard modules (so the capability probe / re-find matcher still resolve).
    stop_entries = _stop_entries(settings)
    assert len(stop_entries) == 1
    assert "matcher" not in stop_entries[0]  # Stop has no tool to match
    fused_command = stop_entries[0]["hooks"][0]["command"]
    for guard in (_SKILL_RETURN_STOP_MODULE, _RENDEZVOUS_STOP_MODULE, _RELAY_AUDIT_MODULE):
        assert guard in fused_command


# ─── idempotency ────────────────────────────────────────────────────────────


def test_rerun_does_not_duplicate_either_entry(tmp_path: Path) -> None:
    first = install_agent_assets(claude_dir=tmp_path)
    assert first["settings_hook"]["action"] == "added"
    assert first["settings_stop_multiplex_hook"]["action"] == "added"

    second = install_agent_assets(claude_dir=tmp_path)
    assert second["settings_hook"]["action"] == "already-present"
    assert second["settings_hook"]["wrote"] is False
    assert second["settings_stop_multiplex_hook"]["action"] == "already-present"
    assert second["settings_stop_multiplex_hook"]["wrote"] is False

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

    # Unrelated keys preserved verbatim. ``permissions.deny`` keeps its
    # pre-existing entry; the host-scoped raw-ssh/scp deny rules
    # (_merge_deny_rules) are exercised in the dedicated
    # test_agent_assets_settings_deny.py module (they depend on the resolved
    # cluster hosts, so they are pinned there hermetically, not here).
    # ``permissions.allow`` is augmented with Skill(<name>) entries by the
    # sibling _merge_skill_permissions step.
    assert "Bash(rm -rf:*)" in settings["permissions"]["deny"]
    # The over-broad blanket rule is NEVER written (narrowed 2026-07-10).
    assert "Bash(ssh:*)" not in settings["permissions"]["deny"]
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
    assert result["settings_stop_multiplex_hook"]["action"] == "skipped-unparseable"
    # The original (invalid) content is preserved untouched.
    assert settings_path.read_text(encoding="utf-8") == "{ this is not valid json"


def test_non_object_settings_is_not_clobbered(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    result = install_agent_assets(claude_dir=tmp_path)

    assert result["settings_hook"]["action"] == "skipped-unparseable"
    assert result["settings_stop_multiplex_hook"]["action"] == "skipped-unparseable"
    assert json.loads(settings_path.read_text(encoding="utf-8")) == [1, 2, 3]


# ─── dry-run writes nothing ─────────────────────────────────────────────────


def test_dry_run_does_not_write_settings(tmp_path: Path) -> None:
    result = install_agent_assets(claude_dir=tmp_path, dry_run=True)

    assert result["settings_hook"]["action"] == "dry-run-would-add"
    assert result["settings_hook"]["wrote"] is False
    assert result["settings_stop_multiplex_hook"]["action"] == "dry-run-would-add"
    assert result["settings_stop_multiplex_hook"]["wrote"] is False
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


# ─── Fused Stop hook (stop_multiplex) + legacy migration (#288) ─────────────


def test_fused_stop_hook_is_a_single_entry_naming_all_three_guards(tmp_path: Path) -> None:
    """The single fused Stop entry carries the relay-audit needle (so the
    capability probe still resolves it) AND the other two guard needles."""
    result = install_agent_assets(claude_dir=tmp_path)
    assert result["settings_stop_multiplex_hook"]["action"] == "added"
    assert result["settings_stop_multiplex_hook"]["wrote"] is True
    assert result["settings_stop_multiplex_hook"]["removed_legacy"] == []

    settings = _settings(tmp_path)
    stop = settings["hooks"]["Stop"]
    # Exactly one Stop entry — the multiplex — and it is the only one bearing
    # each guard needle (no standalone entries).
    assert len(stop) == 1
    assert len(_entries_with_module(stop, _STOP_MODULE)) == 1
    for guard in (_SKILL_RETURN_STOP_MODULE, _RENDEZVOUS_STOP_MODULE, _RELAY_AUDIT_MODULE):
        assert len(_entries_with_module(stop, guard)) == 1
    assert "matcher" not in stop[0]  # Stop has no tool to match

    # Idempotent on re-run.
    second = install_agent_assets(claude_dir=tmp_path)
    assert second["settings_stop_multiplex_hook"]["action"] == "already-present"
    assert len(_settings(tmp_path)["hooks"]["Stop"]) == 1


def test_legacy_three_standalone_stop_entries_migrate_to_one_multiplex(tmp_path: Path) -> None:
    """A pre-fusion settings.json with the THREE standalone Stop guards ends with
    exactly one multiplex entry and NONE of the three legacy standalone entries
    (the 539c1cdc regression zone: an upgrade must never leave a duplicate)."""
    settings_path = tmp_path / "settings.json"

    def _legacy(module: str) -> dict:
        return {"hooks": [{"type": "command", "command": f"/old/venv/python -m {module}"}]}

    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "echo bye"}]},  # user's own
                        _legacy(_SKILL_RETURN_STOP_MODULE),
                        _legacy(_RENDEZVOUS_STOP_MODULE),
                        _legacy(_RELAY_AUDIT_MODULE),
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    result = install_agent_assets(claude_dir=tmp_path)
    assert result["settings_stop_multiplex_hook"]["action"] in ("added", "updated")
    assert result["settings_stop_multiplex_hook"]["wrote"] is True
    # All three legacy guard needles were dropped in the same write.
    assert set(result["settings_stop_multiplex_hook"]["removed_legacy"]) == {
        _SKILL_RETURN_STOP_MODULE,
        _RENDEZVOUS_STOP_MODULE,
        _RELAY_AUDIT_MODULE,
    }

    stop = _settings(tmp_path)["hooks"]["Stop"]
    # The user's own entry survives; exactly one fused entry; NO standalone legacy
    # entries remain (each guard needle appears in exactly the one fused command).
    assert {"hooks": [{"type": "command", "command": "echo bye"}]} in stop
    assert len(_entries_with_module(stop, _STOP_MODULE)) == 1
    for guard in (_SKILL_RETURN_STOP_MODULE, _RENDEZVOUS_STOP_MODULE, _RELAY_AUDIT_MODULE):
        # The guard needle appears only inside the fused entry, never a standalone.
        bearing = _entries_with_module(stop, guard)
        assert len(bearing) == 1
        assert _STOP_MODULE in bearing[0]["hooks"][0]["command"]

    # Idempotent + stable: a second install neither re-adds nor re-removes.
    second = install_agent_assets(claude_dir=tmp_path)
    assert second["settings_stop_multiplex_hook"]["action"] == "already-present"
    assert second["settings_stop_multiplex_hook"]["removed_legacy"] == []


def test_stale_multiplex_command_is_healed_in_place(tmp_path: Path) -> None:
    """A fused entry from a moved venv (stale interpreter path) is updated in
    place, not duplicated."""
    install_agent_assets(claude_dir=tmp_path)
    settings_path = tmp_path / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    # Corrupt the interpreter path of the fused Stop command.
    stop_cmd = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
    settings["hooks"]["Stop"][0]["hooks"][0]["command"] = stop_cmd.replace(
        stop_cmd.split(" -m ", 1)[0], "/moved/venv/python"
    )
    settings_path.write_text(json.dumps(settings), encoding="utf-8")

    result = install_agent_assets(claude_dir=tmp_path)
    assert result["settings_stop_multiplex_hook"]["action"] == "updated"
    stop = _settings(tmp_path)["hooks"]["Stop"]
    assert len(_entries_with_module(stop, _STOP_MODULE)) == 1
