"""Fire-path tests for ``scripts/check_handoff_disjointness.py``.

The checker guards the three swarm-dispatch failure modes: same-wave file
collisions (unchecked convergence), typo'd claims (silent drift), and
worktree-intersecting claims at dispatch (in-flight overlap). These tests plant
each red condition, confirm the clean case is green, and pin the verdicts of the
two real handoff packages in the tree.
"""

from __future__ import annotations

import json
from pathlib import Path

import scripts.check_handoff_disjointness as chk

REPO = chk.REPO
LATENCY = REPO / "docs" / "plans" / "latency-elimination-2026-07-16" / "unit-specs.json"
DAEMON = REPO / "docs" / "plans" / "daemon-engineering-2026-07-16" / "unit-specs.json"


def _write(tmp_path: Path, specs: dict) -> Path:
    p = tmp_path / "unit-specs.json"
    p.write_text(json.dumps(specs), encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# entry parsing
# --------------------------------------------------------------------------- #
def test_parse_entry_kinds() -> None:
    assert chk.parse_entry("scripts/foo.py (new)") == chk.Entry(
        "scripts/foo.py (new)", "scripts/foo.py", "file", True
    )
    assert chk.parse_entry("tests/daemon/").kind == "dir"
    assert chk.parse_entry("tests/infra/test_io*.py").kind == "glob"
    # Prose survives: embedded space, or bare token with no extension.
    assert chk.parse_entry("slash_commands twin of SKILL.md").kind == "prose"
    assert chk.parse_entry("doctor module").kind == "prose"
    # Annotation that is not "new" does not set is_new.
    e = chk.parse_entry("infra/cluster_status.py (EXCLUSIVE)")
    assert e.path == "infra/cluster_status.py" and e.is_new is False


# --------------------------------------------------------------------------- #
# (a) same-wave overlap = ERROR
# --------------------------------------------------------------------------- #
def test_same_wave_collision_is_red(tmp_path: Path) -> None:
    specs = {
        "units": [
            {"unit_id": "A", "wave": 1, "files": ["src/hpc_agent/shared.py"]},
            {"unit_id": "B", "wave": 1, "files": ["src/hpc_agent/shared.py"]},
        ]
    }
    rep = chk.check_spec_file(_write(tmp_path, specs), tmp_path)
    assert not rep.ok
    assert any("same-wave collision" in e and "shared.py" in e for e in rep.errors)


def test_cross_wave_overlap_is_only_warn(tmp_path: Path) -> None:
    specs = {
        "units": [
            {"unit_id": "A", "wave": 1, "files": ["src/hpc_agent/seam.py"]},
            {"unit_id": "B", "wave": 2, "files": ["src/hpc_agent/seam.py"]},
        ]
    }
    rep = chk.check_spec_file(_write(tmp_path, specs), tmp_path)
    assert rep.ok  # cross-wave sequencing is legal
    assert any("cross-wave overlap" in w and "wave 1" in w and "wave 2" in w for w in rep.warns)


def test_same_wave_directory_containment_is_warn(tmp_path: Path) -> None:
    specs = {
        "units": [
            {"unit_id": "DIR", "wave": "DW1", "files": ["tests/daemon/"]},
            {"unit_id": "FILE", "wave": "DW1", "files": ["tests/daemon/test_x.py (new)"]},
        ]
    }
    rep = chk.check_spec_file(_write(tmp_path, specs), tmp_path)
    assert rep.ok  # containment is a smell, not a hard collision
    assert any("containment" in w and "DIR" in w and "FILE" in w for w in rep.warns)


# --------------------------------------------------------------------------- #
# (b) path reality / typo
# --------------------------------------------------------------------------- #
def test_typod_path_resembling_real_sibling_is_red(tmp_path: Path) -> None:
    # A near-duplicate of a real repo file (dropped the 'u' in 'truth') — the
    # confident-typo class: parent dir exists, sibling is edit-distance 1 away.
    specs = {
        "units": [
            {
                "unit_id": "T",
                "wave": 1,
                "files": ["scripts/lint_parser_bake_trth.py"],
            }
        ]
    }
    rep = chk.check_spec_file(_write(tmp_path, specs), REPO)
    assert not rep.ok
    assert any("typo" in e.lower() and "lint_parser_bake_truth.py" in e for e in rep.errors)


def test_genuinely_new_file_marked_new_is_silent(tmp_path: Path) -> None:
    specs = {"units": [{"unit_id": "N", "wave": 1, "files": ["scripts/brand_new_lint.py (new)"]}]}
    rep = chk.check_spec_file(_write(tmp_path, specs), REPO)
    assert rep.ok
    assert not rep.warns


def test_unmarked_new_file_in_new_subtree_warns_not_errors(tmp_path: Path) -> None:
    # A new file in a not-yet-created directory (daemon's pattern): no sibling to
    # resemble, so it WARNs (plausibly new) rather than erroring (typo).
    specs = {
        "units": [
            {
                "unit_id": "D",
                "wave": 1,
                "files": ["src/hpc_agent/_kernel/extension/daemon/transport.py"],
            }
        ]
    }
    rep = chk.check_spec_file(_write(tmp_path, specs), REPO)
    assert rep.ok
    assert any("not in the tree" in w for w in rep.warns)


def test_prose_entries_are_skipped(tmp_path: Path) -> None:
    specs = {
        "units": [
            {
                "unit_id": "P",
                "wave": 1,
                "files": ["doctor module (jsonschema-importable probe, locate at build)"],
            }
        ]
    }
    rep = chk.check_spec_file(_write(tmp_path, specs), REPO)
    assert rep.ok and not rep.warns


# --------------------------------------------------------------------------- #
# (c) worktree intersection = ERROR (dispatch gate)
# --------------------------------------------------------------------------- #
def test_worktree_intersection_is_listed_red(tmp_path: Path) -> None:
    specs = {
        "units": [
            {"unit_id": "A", "wave": 1, "files": ["src/hpc_agent/target.py (new)"]},
            {"unit_id": "B", "wave": 1, "files": ["src/hpc_agent/other.py (new)"]},
        ]
    }
    dirty = ["src/hpc_agent/target.py", "README.md"]
    rep = chk.check_spec_file(
        _write(tmp_path, specs), tmp_path, against_worktree=True, worktree_files=dirty
    )
    assert not rep.ok
    assert any("worktree file" in e and "target.py" in e and "unit A" in e for e in rep.errors)
    # The unrelated dirty file (README.md) is not claimed -> not flagged.
    assert not any("README.md" in e for e in rep.errors)


def test_worktree_directory_claim_catches_file_under_it(tmp_path: Path) -> None:
    specs = {"units": [{"unit_id": "D", "wave": 1, "files": ["tests/daemon/ (new)"]}]}
    rep = chk.check_spec_file(
        _write(tmp_path, specs),
        tmp_path,
        against_worktree=True,
        worktree_files=["tests/daemon/test_soak.py"],
    )
    assert not rep.ok
    assert any("tests/daemon/test_soak.py" in e for e in rep.errors)


def test_clean_worktree_is_green(tmp_path: Path) -> None:
    specs = {"units": [{"unit_id": "A", "wave": 1, "files": ["src/hpc_agent/x.py (new)"]}]}
    rep = chk.check_spec_file(
        _write(tmp_path, specs), tmp_path, against_worktree=True, worktree_files=["README.md"]
    )
    assert rep.ok


def test_porcelain_z_parse_includes_rename_source() -> None:
    # "R  new\0old\0 M  mod\0" — rename reports BOTH new and old paths.
    payload = "R  new_path.py\x00old_path.py\x00 M src/mod.py\x00"
    assert chk._parse_porcelain_z(payload) == ["new_path.py", "old_path.py", "src/mod.py"]


# --------------------------------------------------------------------------- #
# clean package = green
# --------------------------------------------------------------------------- #
def test_fully_disjoint_package_is_green(tmp_path: Path) -> None:
    specs = {
        "units": [
            {"unit_id": "1", "wave": 1, "files": ["src/hpc_agent/a.py (new)"]},
            {"unit_id": "2", "wave": 1, "files": ["src/hpc_agent/b.py (new)"]},
            {"unit_id": "3", "wave": 2, "files": ["src/hpc_agent/c.py (new)"]},
        ]
    }
    rep = chk.check_spec_file(_write(tmp_path, specs), tmp_path)
    assert rep.ok and not rep.warns


def test_template_dir_is_skipped_by_discovery() -> None:
    found = chk.discover_spec_files(REPO)
    assert not any("_TEMPLATE" in p.parent.name for p in found)
    # The two real packages ARE discovered.
    names = {p.parent.name for p in found}
    assert "latency-elimination-2026-07-16" in names
    assert "daemon-engineering-2026-07-16" in names


# --------------------------------------------------------------------------- #
# real-package verdicts pinned
# --------------------------------------------------------------------------- #
def test_latency_package_is_green_with_cross_wave_warns() -> None:
    rep = chk.check_spec_file(LATENCY, REPO)
    assert rep.ok, f"latency package unexpectedly red: {rep.errors}"
    # Its many shared seams are all cross-wave (sequenced), never same-wave.
    assert not any("same-wave collision" in w for w in rep.warns)
    assert any("cross-wave overlap" in w for w in rep.warns)


def test_daemon_package_is_green_with_containment_warn() -> None:
    rep = chk.check_spec_file(DAEMON, REPO)
    assert rep.ok, f"daemon package unexpectedly red: {rep.errors}"
    # The one coordination smell: D-CORE's tests/daemon/ contains D-CLIENT's file.
    assert any("containment" in w and "D-CORE" in w for w in rep.warns)


def test_main_on_real_packages_exits_zero() -> None:
    assert chk.main([str(LATENCY)]) == 0
    assert chk.main([str(DAEMON)]) == 0


def test_main_exits_nonzero_on_planted_collision(tmp_path: Path) -> None:
    specs = {
        "units": [
            {"unit_id": "A", "wave": 1, "files": ["src/x.py (new)"]},
            {"unit_id": "B", "wave": 1, "files": ["src/x.py (new)"]},
        ]
    }
    assert chk.main([str(_write(tmp_path, specs))]) == 1
