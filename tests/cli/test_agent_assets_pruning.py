"""Tests for manifest-stamped pruning of removed/renamed assets (#F34).

``_install_tree`` is copy-only, so before this an asset a release DELETED (the
§6 worker removal dropped the ``hpc-worker`` agent) stayed installed forever —
Claude Code kept discovering the stale skill/agent and its ``Skill(...)`` grant.
The install now stamps a manifest of the names it owns and, on the next install,
prunes the owned names the current tree no longer ships (never a user's own
asset). These pin the fire path: a stale-owned asset is removed and its grant
dropped, while a user's own asset and a still-shipped asset survive.
"""

from __future__ import annotations

import json
from pathlib import Path

from hpc_agent.agent_assets import (
    _ASSET_MANIFEST_NAME,
    _prune_stale_assets,
    _write_asset_manifest,
    install_agent_assets,
)


def _seed_manifest(claude_dir: Path, **owned: list[str]) -> None:
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / _ASSET_MANIFEST_NAME).write_text(
        json.dumps({"version": "test", **owned}), encoding="utf-8"
    )


def test_prune_removes_owned_asset_absent_from_current_tree(tmp_path: Path) -> None:
    """FIRE PATH: a skill/agent the manifest owned but the current tree drops is deleted."""
    # A prior install owned ``ghost-skill`` + ``hpc-worker`` (agent); the on-disk
    # artifacts and the manifest stamp exist.
    _seed_manifest(tmp_path, skills=["ghost-skill", "hpc-submit"], agents=["hpc-worker"])
    ghost = tmp_path / "skills" / "ghost-skill"
    ghost.mkdir(parents=True)
    (ghost / "SKILL.md").write_text("stale", encoding="utf-8")
    worker = tmp_path / "agents" / "hpc-worker.md"
    worker.parent.mkdir(parents=True)
    worker.write_text("stale agent", encoding="utf-8")
    # A user's OWN hand-added skill was never stamped — it must survive.
    mine = tmp_path / "skills" / "my-own-skill"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("mine", encoding="utf-8")

    pruned = _prune_stale_assets(
        tmp_path,
        current={"commands": set(), "skills": {"hpc-submit"}, "agents": set()},
        dry_run=False,
    )

    assert pruned["skills"] == ["ghost-skill"]
    assert pruned["agents"] == ["hpc-worker"]
    assert not ghost.exists()  # stale skill removed
    assert not worker.exists()  # stale agent removed
    assert mine.exists()  # user's own asset untouched (never stamped)


def test_prune_is_a_noop_without_a_prior_manifest(tmp_path: Path) -> None:
    """A first install (no manifest) prunes nothing — copy-only behaviour preserved."""
    (tmp_path / "skills" / "hpc-submit").mkdir(parents=True)
    pruned = _prune_stale_assets(
        tmp_path,
        current={"commands": set(), "skills": {"hpc-submit"}, "agents": set()},
        dry_run=False,
    )
    assert pruned == {"commands": [], "skills": [], "agents": []}


def test_prune_dry_run_reports_without_deleting(tmp_path: Path) -> None:
    _seed_manifest(tmp_path, skills=["ghost-skill"])
    ghost = tmp_path / "skills" / "ghost-skill"
    ghost.mkdir(parents=True)
    pruned = _prune_stale_assets(
        tmp_path, current={"commands": set(), "skills": set(), "agents": set()}, dry_run=True
    )
    assert pruned["skills"] == ["ghost-skill"]
    assert ghost.exists()  # dry-run deletes nothing


def test_install_prunes_stale_skill_and_drops_its_grant(tmp_path: Path) -> None:
    """End-to-end fire path through the public entrypoint.

    Pre-seed a manifest + on-disk ghost skill + its ``Skill(ghost-skill)`` allow
    rule, then run a real ``install_agent_assets`` (whose bundled tree has no
    ``ghost-skill``): the ghost is pruned, its grant dropped, and a genuinely
    bundled skill (``hpc-submit``) and its grant survive.
    """
    # Seed the ghost as previously-owned.
    _seed_manifest(tmp_path, skills=["ghost-skill", "hpc-submit"])
    ghost = tmp_path / "skills" / "ghost-skill"
    ghost.mkdir(parents=True)
    (ghost / "SKILL.md").write_text("stale", encoding="utf-8")
    (tmp_path / "settings.json").write_text(
        json.dumps({"permissions": {"allow": ["Skill(ghost-skill)", "Skill(hpc-submit)"]}}),
        encoding="utf-8",
    )

    result = install_agent_assets(claude_dir=tmp_path)

    assert "ghost-skill" in result["assets_pruned"]["skills"]
    assert not ghost.exists()
    assert "Skill(ghost-skill)" in result["settings_permissions_pruned"]["removed"]

    allow = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    allow_rules = allow["permissions"]["allow"]
    assert "Skill(ghost-skill)" not in allow_rules  # stale grant dropped
    assert "Skill(hpc-submit)" in allow_rules  # bundled skill's grant survives
    # The new manifest re-stamps current ownership (ghost gone, hpc-submit kept).
    manifest = json.loads((tmp_path / _ASSET_MANIFEST_NAME).read_text(encoding="utf-8"))
    assert "ghost-skill" not in manifest["skills"]
    assert "hpc-submit" in manifest["skills"]


def test_write_manifest_stamps_owned_names(tmp_path: Path) -> None:
    report = _write_asset_manifest(
        tmp_path, commands={"a"}, skills={"s"}, agents=set(), dry_run=False
    )
    assert report["wrote"] is True
    data = json.loads((tmp_path / _ASSET_MANIFEST_NAME).read_text(encoding="utf-8"))
    assert data["commands"] == ["a"]
    assert data["skills"] == ["s"]
    assert data["agents"] == []
    assert "version" in data
