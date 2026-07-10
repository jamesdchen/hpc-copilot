"""Tests for the settings.json merge that DENIES raw ssh/scp Bash invocations.

``install-commands`` / ``setup`` adds ``Bash(ssh:*)`` and ``Bash(scp:*)`` to
``permissions.deny`` (anti-vendor-lockout ruling (a), 2026-07-10): the
improvisation class — an agent hand-rolling raw ssh/scp against a cluster host —
dies at the permission layer, while the sanctioned hpc-agent verbs (which dial
ssh inside their own processes, never via the agent's Bash tool) stay
unaffected.

Pins every branch of :func:`hpc_agent.agent_assets._merge_deny_rules` through
the public :func:`install_agent_assets` entrypoint: fresh install, idempotency,
partial overlap, unrelated permissions preserved, unparseable skip, dry-run.
"""

from __future__ import annotations

import json
from pathlib import Path

from hpc_agent.agent_assets import install_agent_assets


def _settings(claude_dir: Path) -> dict:
    data: dict = json.loads((claude_dir / "settings.json").read_text(encoding="utf-8"))
    return data


def _deny(settings: dict) -> list:
    deny: list = settings.get("permissions", {}).get("deny", [])
    return deny


# ─── fresh install ──────────────────────────────────────────────────────────


def test_fresh_install_adds_raw_ssh_deny_rules(tmp_path: Path) -> None:
    result = install_agent_assets(claude_dir=tmp_path)

    deny_report = result["settings_deny"]
    assert deny_report["action"] == "added"
    assert deny_report["wrote"] is True
    assert "Bash(ssh:*)" in deny_report["added"]
    assert "Bash(scp:*)" in deny_report["added"]

    deny = _deny(_settings(tmp_path))
    assert "Bash(ssh:*)" in deny
    assert "Bash(scp:*)" in deny


# ─── idempotency ────────────────────────────────────────────────────────────


def test_rerun_does_not_duplicate_deny_rules(tmp_path: Path) -> None:
    first = install_agent_assets(claude_dir=tmp_path)
    assert first["settings_deny"]["action"] == "added"
    first_deny = list(_deny(_settings(tmp_path)))

    second = install_agent_assets(claude_dir=tmp_path)
    assert second["settings_deny"]["action"] == "already-present"
    assert second["settings_deny"]["wrote"] is False
    assert second["settings_deny"]["added"] == []

    # No churn, no duplicate entries.
    assert _deny(_settings(tmp_path)) == first_deny
    assert first_deny.count("Bash(ssh:*)") == 1


# ─── partial overlap ────────────────────────────────────────────────────────


def test_partial_overlap_adds_only_missing_deny_rule(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"permissions": {"deny": ["Bash(ssh:*)"]}}),  # ssh already denied
        encoding="utf-8",
    )

    result = install_agent_assets(claude_dir=tmp_path)

    deny_report = result["settings_deny"]
    assert deny_report["action"] == "added"
    assert deny_report["added"] == ["Bash(scp:*)"]  # only the missing one

    deny = _deny(_settings(tmp_path))
    assert deny.count("Bash(ssh:*)") == 1
    assert "Bash(scp:*)" in deny


# ─── unrelated permission config preserved ─────────────────────────────────


def test_preserves_existing_allow_and_other_deny(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    pre = {
        "permissions": {
            "allow": ["Bash(hpc-agent:*)", "Skill(hpc-submit)"],
            "deny": ["Bash(rm -rf:*)"],
        },
        "theme": "dark",
    }
    settings_path.write_text(json.dumps(pre), encoding="utf-8")

    install_agent_assets(claude_dir=tmp_path)

    settings = _settings(tmp_path)
    assert settings["theme"] == "dark"
    # Pre-existing allow grants untouched (the sanctioned verbs stay reachable).
    assert "Bash(hpc-agent:*)" in settings["permissions"]["allow"]
    assert "Skill(hpc-submit)" in settings["permissions"]["allow"]
    # Pre-existing deny entry preserved, our raw-ssh rules appended.
    deny = settings["permissions"]["deny"]
    assert "Bash(rm -rf:*)" in deny
    assert "Bash(ssh:*)" in deny
    assert "Bash(scp:*)" in deny


# ─── safety: unparseable settings.json is not clobbered ────────────────────


def test_unparseable_settings_is_skipped(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("not valid json {{{", encoding="utf-8")

    result = install_agent_assets(claude_dir=tmp_path)

    deny_report = result["settings_deny"]
    assert deny_report["action"] == "skipped-unparseable"
    assert deny_report["wrote"] is False
    assert deny_report["added"] == []
    assert settings_path.read_text(encoding="utf-8") == "not valid json {{{"


# ─── dry-run reports without writing ───────────────────────────────────────


def test_dry_run_does_not_write(tmp_path: Path) -> None:
    result = install_agent_assets(claude_dir=tmp_path, dry_run=True)

    deny_report = result["settings_deny"]
    assert deny_report["action"] == "dry-run-would-add"
    assert deny_report["wrote"] is False
    assert "Bash(ssh:*)" in deny_report["added"]
    assert not (tmp_path / "settings.json").exists()
