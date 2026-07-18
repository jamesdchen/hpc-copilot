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
    _LEGACY_OWNED,
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
    assert pruned == {
        "commands": [],
        "skills": [],
        "agents": [],
        "legacy_name_skipped": [],
    }


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


def test_prune_sweeps_legacy_pre_manifest_orphan(tmp_path: Path) -> None:
    """FIRE PATH: a _LEGACY_OWNED orphan is swept even with NO prior manifest.

    The preflight → hpc-preflight incident: a ``commands/preflight.md`` orphan
    (and the retired ``hpc-preflight`` skill) that no manifest ever owned is
    still pruned, because the curated legacy set reaches names the manifest
    subtraction cannot.
    """
    assert "preflight" in _LEGACY_OWNED["commands"]
    assert "hpc-preflight" in _LEGACY_OWNED["skills"]
    # Both orphans carry an hpc-agent authorship marker (the retired assets are
    # known content — they drive hpc-agent machinery, so they name it), so the
    # legacy sweep's authorship gate lets them through.
    orphan = tmp_path / "commands" / "preflight.md"
    orphan.parent.mkdir(parents=True)
    orphan.write_text("Invoke the `hpc-preflight` skill", encoding="utf-8")
    dead_skill = tmp_path / "skills" / "hpc-preflight"
    dead_skill.mkdir(parents=True)
    (dead_skill / "SKILL.md").write_text("Retired hpc-agent preflight skill.", encoding="utf-8")

    pruned = _prune_stale_assets(
        tmp_path,
        current={"commands": {"submit-hpc"}, "skills": {"hpc-submit"}, "agents": set()},
        dry_run=False,
    )

    assert "preflight" in pruned["commands"]
    assert "hpc-preflight" in pruned["skills"]
    assert not orphan.exists()
    assert not dead_skill.exists()


def test_prune_spares_legacy_name_owned_by_current_tree(tmp_path: Path) -> None:
    """A legacy-owned name the CURRENT install re-ships is never swept.

    ``sync`` is in ``_LEGACY_OWNED["commands"]``; if a future tree ships a
    command by that name, the current-ownership guard spares the on-disk file.
    """
    assert "sync" in _LEGACY_OWNED["commands"]
    keep = tmp_path / "commands" / "sync.md"
    keep.parent.mkdir(parents=True)
    keep.write_text("current install owns this", encoding="utf-8")

    pruned = _prune_stale_assets(
        tmp_path,
        current={"commands": {"sync"}, "skills": set(), "agents": set()},
        dry_run=False,
    )

    assert "sync" not in pruned["commands"]
    assert keep.exists()  # current-owned name is never touched


def test_legacy_sweep_prunes_hpc_authored_orphan(tmp_path: Path) -> None:
    """AUTHORSHIP GATE (a): a legacy-owned name whose on-disk content carries the
    hpc-agent authorship marker IS an hpc-agent orphan → pruned, no collision."""
    assert "sync" in _LEGACY_OWNED["commands"]
    authored = tmp_path / "commands" / "sync.md"
    authored.parent.mkdir(parents=True)
    authored.write_text("Run `hpc-agent sync` to reconcile the current repo.", encoding="utf-8")

    pruned = _prune_stale_assets(
        tmp_path,
        current={"commands": set(), "skills": set(), "agents": set()},
        dry_run=False,
    )

    assert "sync" in pruned["commands"]  # authored orphan is pruned
    assert not authored.exists()
    assert pruned["legacy_name_skipped"] == []  # no user collision


def test_legacy_sweep_spares_user_authored_same_named_file(tmp_path: Path) -> None:
    """AUTHORSHIP GATE (b): a user's OWN hand-authored file at a legacy-owned name
    (no hpc-agent marker) is NEVER deleted — it is kept and reported in
    ``legacy_name_skipped`` so the human learns of the collision. The sync.md
    incident: a name match alone must not destroy a user's file."""
    assert "sync" in _LEGACY_OWNED["commands"]
    mine = tmp_path / "commands" / "sync.md"
    mine.parent.mkdir(parents=True)
    mine.write_text("# My personal sync notes\nrsync -av ./src/ ./backup/\n", encoding="utf-8")

    pruned = _prune_stale_assets(
        tmp_path,
        current={"commands": set(), "skills": set(), "agents": set()},
        dry_run=False,
    )

    assert "sync" not in pruned["commands"]  # NOT pruned
    assert mine.exists()  # the user's file survives intact
    assert "commands/sync" in pruned["legacy_name_skipped"]  # collision reported


def test_legacy_sweep_fails_open_on_unreadable_asset(tmp_path: Path) -> None:
    """A legacy-owned skill DIR whose ``SKILL.md`` cannot be read is left in place
    (fail-open): an asset we cannot read is never assumed hpc-authored, so it is
    neither deleted nor reported as a user collision."""
    assert "hpc-preflight" in _LEGACY_OWNED["skills"]
    dead = tmp_path / "skills" / "hpc-preflight"
    dead.mkdir(parents=True)  # no SKILL.md → unreadable content

    pruned = _prune_stale_assets(
        tmp_path,
        current={"commands": set(), "skills": set(), "agents": set()},
        dry_run=False,
    )

    assert "hpc-preflight" not in pruned["skills"]  # not deleted
    assert dead.exists()  # fail-open kept it
    assert pruned["legacy_name_skipped"] == []  # unreadable ≠ user collision


def test_is_hpc_authored_marker_discrimination(tmp_path: Path) -> None:
    """The sentinel anchors on the hyphenated ``hpc-agent`` token (+ the generated
    skill idioms); a generic user file that merely says "sync" does not trip it."""
    from hpc_agent.agent_assets import _is_hpc_authored

    assert _is_hpc_authored("Run `hpc-agent sync` from the repo root.")
    assert _is_hpc_authored("Invoke the `hpc-preflight` skill via the Skill tool.")
    assert not _is_hpc_authored("# my sync command\ngit pull && git push\n")
    assert not _is_hpc_authored("Preflight checklist before a flight: fuel, flaps, trim.")


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
