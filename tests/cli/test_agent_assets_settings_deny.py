"""Tests for the settings.json merge that DENIES raw ssh/scp to CLUSTER HOSTS.

``install-commands`` / ``setup`` adds HOST-SCOPED ``Bash(ssh *<host>*)`` /
``Bash(scp *<host>*)`` rules to ``permissions.deny`` (anti-vendor-lockout ruling
(a), 2026-07-10; narrowed the same day per user: "hpc-agent should be a TOOL and
not something that takes over the user's entire workspace"). The improvisation
class — an agent hand-rolling raw ssh/scp against a *configured cluster host* —
dies at the permission layer, while ssh to any other host is untouched and the
sanctioned hpc-agent verbs (which dial ssh inside their own processes, never via
the agent's Bash tool) stay unaffected.

The narrowing also carries a MIGRATION: an earlier install wrote a BLANKET
``Bash(ssh:*)`` / ``Bash(scp:*)`` deny (which blocked ALL ssh everywhere); the
installer now REMOVES exactly those two on every run so an upgrade heals the
over-reach.

Pins every branch of :func:`hpc_agent.agent_assets._merge_deny_rules` and the
host derivation :func:`hpc_agent.agent_assets._configured_cluster_hosts` through
the public :func:`install_agent_assets` entrypoint. Hosts are controlled
hermetically via ``HPC_CLUSTERS_CONFIG`` pointing at a temp clusters.yaml, so
the tests never depend on the box's real cluster config.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hpc_agent.agent_assets import install_agent_assets

# Two known hosts + a placeholder that must be skipped (the bundled template
# ships ``<your_user>``-style placeholders; a ``<placeholder>`` host is not a
# real cluster to scope a deny to).
_ALPHA = "alpha.example.edu"
_BETA = "beta.example.edu"

_ALPHA_RULES = [f"Bash(ssh *{_ALPHA}*)", f"Bash(scp *{_ALPHA}*)"]
_BETA_RULES = [f"Bash(ssh *{_BETA}*)", f"Bash(scp *{_BETA}*)"]
_HOST_RULES = _ALPHA_RULES + _BETA_RULES

_BLANKET = ["Bash(ssh:*)", "Bash(scp:*)"]


def _write_clusters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    """Point HPC_CLUSTERS_CONFIG at a temp clusters.yaml with *body*."""
    cfg = tmp_path / "clusters.yaml"
    cfg.write_text(body, encoding="utf-8")
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(cfg))


def _two_host_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_clusters(
        tmp_path,
        monkeypatch,
        f"""
alpha:
  host: {_ALPHA}
  user: <your_user>
  scheduler: slurm
beta:
  host: {_BETA}
  user: <your_user>
  scheduler: sge
placeholder:
  host: <your_host>
  user: <your_user>
  scheduler: slurm
""",
    )


def _no_host_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Only a placeholder host → no resolvable hosts → no deny rules to add.
    _write_clusters(
        tmp_path,
        monkeypatch,
        """
placeholder:
  host: <your_host>
  user: <your_user>
  scheduler: slurm
""",
    )


def _settings(claude_dir: Path) -> dict:
    data: dict = json.loads((claude_dir / "settings.json").read_text(encoding="utf-8"))
    return data


def _deny(settings: dict) -> list:
    deny: list = settings.get("permissions", {}).get("deny", [])
    return deny


# ─── fresh install: host-scoped rules, placeholder skipped ──────────────────


def test_fresh_install_adds_host_scoped_deny_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _two_host_config(tmp_path, monkeypatch)

    result = install_agent_assets(claude_dir=tmp_path / "claude")

    deny_report = result["settings_deny"]
    assert deny_report["action"] == "added"
    assert deny_report["wrote"] is True
    # Both real hosts scoped, in both ssh + scp forms.
    assert sorted(deny_report["added"]) == sorted(_HOST_RULES)
    assert deny_report["removed"] == []

    deny = _deny(_settings(tmp_path / "claude"))
    for rule in _HOST_RULES:
        assert rule in deny
    # The blanket rules are NEVER written (tool, not takeover), and the
    # placeholder host contributes nothing.
    assert "Bash(ssh:*)" not in deny
    assert "Bash(scp:*)" not in deny
    assert not any("<your_host>" in r for r in deny)


# ─── idempotency ────────────────────────────────────────────────────────────


def test_rerun_does_not_duplicate_deny_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _two_host_config(tmp_path, monkeypatch)
    claude = tmp_path / "claude"

    first = install_agent_assets(claude_dir=claude)
    assert first["settings_deny"]["action"] == "added"
    first_deny = list(_deny(_settings(claude)))

    second = install_agent_assets(claude_dir=claude)
    assert second["settings_deny"]["action"] == "already-present"
    assert second["settings_deny"]["wrote"] is False
    assert second["settings_deny"]["added"] == []
    assert second["settings_deny"]["removed"] == []

    assert _deny(_settings(claude)) == first_deny
    assert first_deny.count(_ALPHA_RULES[0]) == 1


# ─── MIGRATION: an upgrade removes the over-broad blanket rules ─────────────


def test_reinstall_removes_blanket_rules_and_adds_host_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _two_host_config(tmp_path, monkeypatch)
    claude = tmp_path / "claude"
    settings_path = claude / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    # Simulate the pre-narrowing install: blanket rules + an unrelated user rule.
    settings_path.write_text(
        json.dumps({"permissions": {"deny": _BLANKET + ["Bash(rm -rf:*)"]}}),
        encoding="utf-8",
    )

    result = install_agent_assets(claude_dir=claude)

    deny_report = result["settings_deny"]
    assert deny_report["action"] == "added"
    assert sorted(deny_report["added"]) == sorted(_HOST_RULES)
    assert sorted(deny_report["removed"]) == sorted(_BLANKET)

    deny = _deny(_settings(claude))
    # Blanket rules gone.
    assert "Bash(ssh:*)" not in deny
    assert "Bash(scp:*)" not in deny
    # Host-scoped rules present.
    for rule in _HOST_RULES:
        assert rule in deny
    # The unrelated user deny entry survives — removal is exact-string, blanket-only.
    assert "Bash(rm -rf:*)" in deny


# ─── no configured hosts: no deny rules, other entries untouched ────────────


def test_no_hosts_adds_no_deny_and_preserves_other_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_host_config(tmp_path, monkeypatch)
    claude = tmp_path / "claude"
    settings_path = claude / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps({"permissions": {"deny": ["Bash(rm -rf:*)"]}}),
        encoding="utf-8",
    )

    result = install_agent_assets(claude_dir=claude)

    deny_report = result["settings_deny"]
    # Nothing to add (no hosts), nothing to remove (no blanket present).
    assert deny_report["action"] == "already-present"
    assert deny_report["added"] == []
    assert deny_report["removed"] == []

    deny = _deny(_settings(claude))
    assert deny == ["Bash(rm -rf:*)"]  # untouched
    assert not any("Bash(ssh" in r for r in deny)


def test_no_hosts_still_removes_stale_blanket_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The migration heals the over-reach even on a host-less box."""
    _no_host_config(tmp_path, monkeypatch)
    claude = tmp_path / "claude"
    settings_path = claude / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps({"permissions": {"deny": _BLANKET + ["Bash(rm -rf:*)"]}}),
        encoding="utf-8",
    )

    result = install_agent_assets(claude_dir=claude)

    deny_report = result["settings_deny"]
    assert deny_report["action"] == "updated"  # removal-only
    assert deny_report["added"] == []
    assert sorted(deny_report["removed"]) == sorted(_BLANKET)

    deny = _deny(_settings(claude))
    assert "Bash(ssh:*)" not in deny
    assert "Bash(scp:*)" not in deny
    assert deny == ["Bash(rm -rf:*)"]  # only the unrelated user rule remains


# ─── partial overlap: adds only the missing host rule ───────────────────────


def test_partial_overlap_adds_only_missing_deny_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _two_host_config(tmp_path, monkeypatch)
    claude = tmp_path / "claude"
    settings_path = claude / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    # alpha's ssh rule already present; everything else missing.
    settings_path.write_text(
        json.dumps({"permissions": {"deny": [_ALPHA_RULES[0]]}}),
        encoding="utf-8",
    )

    result = install_agent_assets(claude_dir=claude)

    deny_report = result["settings_deny"]
    assert deny_report["action"] == "added"
    expected_missing = [r for r in _HOST_RULES if r != _ALPHA_RULES[0]]
    assert sorted(deny_report["added"]) == sorted(expected_missing)

    deny = _deny(_settings(claude))
    assert deny.count(_ALPHA_RULES[0]) == 1
    for rule in _HOST_RULES:
        assert rule in deny


# ─── unrelated permission config preserved ─────────────────────────────────


def test_preserves_existing_allow_and_other_deny(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _two_host_config(tmp_path, monkeypatch)
    claude = tmp_path / "claude"
    settings_path = claude / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    pre = {
        "permissions": {
            "allow": ["Bash(hpc-agent:*)", "Skill(hpc-submit)"],
            "deny": ["Bash(rm -rf:*)"],
        },
        "theme": "dark",
    }
    settings_path.write_text(json.dumps(pre), encoding="utf-8")

    install_agent_assets(claude_dir=claude)

    settings = _settings(claude)
    assert settings["theme"] == "dark"
    # Pre-existing allow grants untouched (the sanctioned verbs stay reachable).
    assert "Bash(hpc-agent:*)" in settings["permissions"]["allow"]
    assert "Skill(hpc-submit)" in settings["permissions"]["allow"]
    # Pre-existing deny entry preserved, host-scoped rules appended.
    deny = settings["permissions"]["deny"]
    assert "Bash(rm -rf:*)" in deny
    for rule in _HOST_RULES:
        assert rule in deny


# ─── safety: unparseable settings.json is not clobbered ────────────────────


def test_unparseable_settings_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _two_host_config(tmp_path, monkeypatch)
    claude = tmp_path / "claude"
    settings_path = claude / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text("not valid json {{{", encoding="utf-8")

    result = install_agent_assets(claude_dir=claude)

    deny_report = result["settings_deny"]
    assert deny_report["action"] == "skipped-unparseable"
    assert deny_report["wrote"] is False
    assert deny_report["added"] == []
    assert deny_report["removed"] == []
    assert settings_path.read_text(encoding="utf-8") == "not valid json {{{"


# ─── dry-run reports without writing ───────────────────────────────────────


def test_dry_run_does_not_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _two_host_config(tmp_path, monkeypatch)
    claude = tmp_path / "claude"

    result = install_agent_assets(claude_dir=claude, dry_run=True)

    deny_report = result["settings_deny"]
    assert deny_report["action"] == "dry-run-would-add"
    assert deny_report["wrote"] is False
    assert sorted(deny_report["added"]) == sorted(_HOST_RULES)
    assert not (claude / "settings.json").exists()
