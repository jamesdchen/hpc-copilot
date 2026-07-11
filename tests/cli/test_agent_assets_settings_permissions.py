"""Tests for the settings.json merge that grants Skill(<name>) allow rules.

``install-commands`` / ``setup`` adds a ``Skill(<name>)`` entry to
``permissions.allow`` for every installed skill, so Claude Code's auto-mode
classifier stops silently denying the first ``Skill(hpc-submit)`` /
``Skill(hpc-aggregate)`` / etc. call from the orchestrator slashes.

These pin every branch of
:func:`hpc_agent.agent_assets._merge_skill_permissions` through the public
:func:`install_agent_assets` entrypoint: fresh install, idempotency,
partial overlap, unrelated permissions preserved, unparseable skip, and
dry-run.
"""

from __future__ import annotations

import json
from pathlib import Path

from hpc_agent.agent_assets import install_agent_assets


def _settings(claude_dir: Path) -> dict:
    data: dict = json.loads((claude_dir / "settings.json").read_text(encoding="utf-8"))
    return data


def _allow(settings: dict) -> list:
    allow: list = settings.get("permissions", {}).get("allow", [])
    return allow


# ─── fresh install: no settings.json yet ────────────────────────────────────


def test_fresh_install_adds_skill_allow_rules(tmp_path: Path) -> None:
    result = install_agent_assets(claude_dir=tmp_path)

    perms = result["settings_permissions"]
    assert perms["action"] == "added"
    assert perms["wrote"] is True
    assert len(perms["added"]) >= 1
    # Every entry is the Skill(<name>) parameterised matcher form
    assert all(rule.startswith("Skill(") and rule.endswith(")") for rule in perms["added"])

    # Each installed skill has its own allow rule
    allow = _allow(_settings(tmp_path))
    for skill_name in result["skills_installed"]:
        assert f"Skill({skill_name})" in allow


# ─── idempotency ────────────────────────────────────────────────────────────


def test_rerun_does_not_duplicate_rules(tmp_path: Path) -> None:
    first = install_agent_assets(claude_dir=tmp_path)
    assert first["settings_permissions"]["action"] == "added"
    first_allow = list(_allow(_settings(tmp_path)))

    second = install_agent_assets(claude_dir=tmp_path)
    assert second["settings_permissions"]["action"] == "already-present"
    assert second["settings_permissions"]["wrote"] is False
    assert second["settings_permissions"]["added"] == []

    # No churn in the allow list
    assert _allow(_settings(tmp_path)) == first_allow


# ─── partial overlap with pre-existing config ──────────────────────────────


def test_partial_overlap_adds_only_missing_rules(tmp_path: Path) -> None:
    """A pre-existing settings.json with SOME rules present gets only the missing ones."""
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "permissions": {
                    "allow": [
                        "Bash(hpc-agent:*)",
                        "Skill(hpc-submit)",  # already granted
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    result = install_agent_assets(claude_dir=tmp_path)

    perms = result["settings_permissions"]
    assert perms["action"] == "added"
    assert perms["wrote"] is True
    # hpc-submit was already there → not in the added list
    assert "Skill(hpc-submit)" not in perms["added"]
    # other bundled skills are
    skills_installed = result["skills_installed"]
    if "hpc-aggregate" in skills_installed:
        assert "Skill(hpc-aggregate)" in perms["added"]

    allow = _allow(_settings(tmp_path))
    # Pre-existing entries preserved
    assert "Bash(hpc-agent:*)" in allow
    assert "Skill(hpc-submit)" in allow
    # All bundled skills now granted
    for name in skills_installed:
        assert f"Skill({name})" in allow


def test_preserves_unrelated_permission_keys(tmp_path: Path) -> None:
    """deny rules, other keys, and unrelated allow entries are untouched."""
    settings_path = tmp_path / "settings.json"
    pre = {
        "permissions": {
            "allow": ["Bash(echo:*)"],
            "deny": ["Bash(rm -rf:*)"],
        },
        "theme": "dark",
    }
    settings_path.write_text(json.dumps(pre), encoding="utf-8")

    install_agent_assets(claude_dir=tmp_path)

    settings = _settings(tmp_path)
    assert settings["theme"] == "dark"
    # Pre-existing deny entry preserved; the host-scoped raw-ssh/scp deny rules
    # are covered in test_agent_assets_settings_deny.py. The over-broad blanket
    # rule is NEVER written (narrowed 2026-07-10 — tool, not takeover).
    deny = settings["permissions"]["deny"]
    assert "Bash(rm -rf:*)" in deny
    assert "Bash(ssh:*)" not in deny
    # Pre-existing allow entry preserved
    assert "Bash(echo:*)" in settings["permissions"]["allow"]


# ─── safety: unparseable settings.json is not clobbered ────────────────────


def test_unparseable_settings_is_skipped(tmp_path: Path) -> None:
    """An existing-but-unparseable settings.json is left untouched."""
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("not valid json {{{", encoding="utf-8")

    result = install_agent_assets(claude_dir=tmp_path)

    perms = result["settings_permissions"]
    assert perms["action"] == "skipped-unparseable"
    assert perms["wrote"] is False
    assert perms["added"] == []
    # File contents not clobbered
    assert settings_path.read_text(encoding="utf-8") == "not valid json {{{"


# ─── dry-run reports without writing ───────────────────────────────────────


def test_dry_run_does_not_write(tmp_path: Path) -> None:
    """dry-run returns dry-run-would-add and leaves disk untouched."""
    result = install_agent_assets(claude_dir=tmp_path, dry_run=True)

    perms = result["settings_permissions"]
    assert perms["action"] == "dry-run-would-add"
    assert perms["wrote"] is False
    assert len(perms["added"]) >= 1
    assert not (tmp_path / "settings.json").exists()
