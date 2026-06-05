"""Tests for the settings.json merge that wires the skill-return autofetch hook.

``install-commands`` / ``setup`` injects the ``PostToolUse`` autofetch hook
(WS5 PR4) into ``<claude_dir>/settings.json``. The injection MUST be:

* **additive** — never clobber existing hooks, permissions, or any other key;
* **idempotent** — re-running install does not append a duplicate entry;
* **safe** — an existing-but-unparseable settings.json is left untouched.

These pin every branch of :func:`hpc_agent.agent_assets._merge_skill_return_hook`
through the public :func:`install_agent_assets` entrypoint, plus a dry-run that
writes nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

from hpc_agent.agent_assets import install_agent_assets

_HOOK_MODULE = "hpc_agent._kernel.hooks.skill_return_autofetch"


def _settings(claude_dir: Path) -> dict:
    return json.loads((claude_dir / "settings.json").read_text(encoding="utf-8"))


def _post_tool_use(settings: dict) -> list:
    return settings["hooks"]["PostToolUse"]


def _autofetch_entries(settings: dict) -> list:
    out = []
    for entry in _post_tool_use(settings):
        for h in entry.get("hooks", []):
            if _HOOK_MODULE in h.get("command", ""):
                out.append(entry)
    return out


# ─── fresh install: no settings.json yet ────────────────────────────────────


def test_creates_settings_json_when_absent(tmp_path: Path) -> None:
    result = install_agent_assets(claude_dir=tmp_path)

    assert result["settings_hook"]["action"] == "added"
    assert result["settings_hook"]["wrote"] is True

    settings = _settings(tmp_path)
    entries = _autofetch_entries(settings)
    assert len(entries) == 1
    assert entries[0]["matcher"] == "Skill"


# ─── idempotency ────────────────────────────────────────────────────────────


def test_rerun_does_not_duplicate_the_entry(tmp_path: Path) -> None:
    first = install_agent_assets(claude_dir=tmp_path)
    assert first["settings_hook"]["action"] == "added"

    second = install_agent_assets(claude_dir=tmp_path)
    assert second["settings_hook"]["action"] == "already-present"
    assert second["settings_hook"]["wrote"] is False

    settings = _settings(tmp_path)
    assert len(_autofetch_entries(settings)) == 1


def test_stale_hook_command_is_replaced_in_place(tmp_path: Path) -> None:
    """A stale entry (different interpreter path, or pre-0.10.10 broken encoding) is updated.

    Earlier versions emitted a raw ``sys.executable`` Windows backslash path
    that bash mis-interpreted as escape sequences ('C:\\\\U' → 'C:U'), and the
    old idempotency check (module-path-only) treated the broken entry as
    already-present, so ``install-commands`` could not heal it. The merge now
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
                            "matcher": "Skill",
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


def test_byte_equal_entry_is_already_present(tmp_path: Path) -> None:
    """An entry byte-equal to the canonical install short-circuits as already-present."""
    # Bootstrap by running once — the first install writes the canonical entry.
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
        },
        "customKey": {"nested": [1, 2, 3]},
    }
    settings_path.write_text(json.dumps(existing), encoding="utf-8")

    install_agent_assets(claude_dir=tmp_path)
    settings = _settings(tmp_path)

    # Unrelated keys preserved verbatim.
    assert settings["permissions"] == {"deny": ["Bash(rm -rf:*)"]}
    assert settings["customKey"] == {"nested": [1, 2, 3]}
    assert settings["hooks"]["PreToolUse"] == [{"matcher": "Bash", "hooks": []}]

    # The pre-existing PostToolUse entry survives, and ours is appended after it.
    ptu = _post_tool_use(settings)
    assert ptu[0]["matcher"] == "Edit|Write"
    assert ptu[0]["hooks"][0]["command"] == "ruff check"
    assert len(_autofetch_entries(settings)) == 1


def test_creates_post_tool_use_when_hooks_exist_without_it(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"hooks": {"PreToolUse": []}}), encoding="utf-8")

    install_agent_assets(claude_dir=tmp_path)
    settings = _settings(tmp_path)

    assert settings["hooks"]["PreToolUse"] == []
    assert len(_autofetch_entries(settings)) == 1


# ─── safety: refuse to clobber unparseable settings ─────────────────────────


def test_unparseable_settings_is_not_clobbered(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{ this is not valid json", encoding="utf-8")

    result = install_agent_assets(claude_dir=tmp_path)

    assert result["settings_hook"]["action"] == "skipped-unparseable"
    assert result["settings_hook"]["wrote"] is False
    # The original (invalid) content is preserved untouched.
    assert settings_path.read_text(encoding="utf-8") == "{ this is not valid json"


def test_non_object_settings_is_not_clobbered(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    result = install_agent_assets(claude_dir=tmp_path)

    assert result["settings_hook"]["action"] == "skipped-unparseable"
    assert json.loads(settings_path.read_text(encoding="utf-8")) == [1, 2, 3]


# ─── dry-run writes nothing ─────────────────────────────────────────────────


def test_dry_run_does_not_write_settings(tmp_path: Path) -> None:
    result = install_agent_assets(claude_dir=tmp_path, dry_run=True)

    assert result["settings_hook"]["action"] == "dry-run-would-add"
    assert result["settings_hook"]["wrote"] is False
    assert not (tmp_path / "settings.json").exists()
