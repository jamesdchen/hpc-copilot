"""Tests for the generic pack-manifest re-seal (``state/pack_sweep.py``).

Pure hashing over a declarative ``sweep.json`` recipe (DP2 — no pack code runs).
Covers: recipe shape validation (loud), the sorted pack_files+sweep-glob union,
byte-identical canonical serialization, SEMANTIC staleness (whitespace-only churn
is NOT stale; a moved file sha IS), the minimal-set property (editing one file
never marks an unrelated recipe stale), and the reseal write + old/new sha report.
Toy vocabulary only.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent.state import pack_sweep

if TYPE_CHECKING:
    from pathlib import Path


def _raw_sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_pack(pack_root: Path, *, sweep_globs: list[str] | None = None) -> None:
    """A toy pack: a template + a swept doc, with a sweep.json recipe beside it."""
    pack_root.mkdir(parents=True, exist_ok=True)
    (pack_root / "templates").mkdir(exist_ok=True)
    (pack_root / "templates" / "widget_audit.py").write_text("# %% audit\n", encoding="utf-8")
    (pack_root.parent / "writeup").mkdir(parents=True, exist_ok=True)
    (pack_root.parent / "writeup" / "design.md").write_text("# design\n", encoding="utf-8")
    recipe = {
        "name": "toy-widgets",
        "version": "1.0.0",
        "seams": {"audit_template": "templates/widget_audit.py"},
        "fills_slots": ["widget-audit"],
        "pack_files": ["templates/widget_audit.py"],
        "sweep": sweep_globs if sweep_globs is not None else ["../writeup/*.md"],
    }
    (pack_root / "sweep.json").write_text(json.dumps(recipe, indent=2), encoding="utf-8")


def test_load_recipe_shape(tmp_path: Path) -> None:
    _write_pack(tmp_path / "pack")
    recipe = pack_sweep.load_recipe(tmp_path / "pack" / "sweep.json")
    assert recipe.name == "toy-widgets"
    assert recipe.pack_files == ("templates/widget_audit.py",)
    assert recipe.seams == {"audit_template": "templates/widget_audit.py"}


def test_load_recipe_refuses_unknown_seam(tmp_path: Path) -> None:
    (tmp_path).mkdir(exist_ok=True)
    (tmp_path / "sweep.json").write_text(
        json.dumps({"name": "p", "version": "1", "seams": {"not_a_seam": "x"}}),
        encoding="utf-8",
    )
    with pytest.raises(errors.SpecInvalid, match="unknown seam"):
        pack_sweep.load_recipe(tmp_path / "sweep.json")


def test_load_recipe_refuses_non_slug_name(tmp_path: Path) -> None:
    (tmp_path / "sweep.json").write_text(
        json.dumps({"name": "bad name!", "version": "1"}), encoding="utf-8"
    )
    with pytest.raises(errors.SpecInvalid):
        pack_sweep.load_recipe(tmp_path / "sweep.json")


def test_resolve_files_is_sorted_union(tmp_path: Path) -> None:
    pack_root = tmp_path / "pack"
    _write_pack(pack_root)
    recipe = pack_sweep.load_recipe(pack_root / "sweep.json")
    files = pack_sweep.resolve_recipe_files(recipe, pack_root)
    assert files == sorted(files)
    assert "templates/widget_audit.py" in files
    assert any(f.endswith("writeup/design.md") for f in files)


def test_fresh_manifest_dict_matches_raw_shas(tmp_path: Path) -> None:
    pack_root = tmp_path / "pack"
    _write_pack(pack_root)
    recipe = pack_sweep.load_recipe(pack_root / "sweep.json")
    manifest = pack_sweep.fresh_manifest_dict(recipe, pack_root)
    by_path = {f["path"]: f["sha256"] for f in manifest["files"]}
    tmpl_sha = _raw_sha((pack_root / "templates" / "widget_audit.py").read_bytes())
    assert by_path["templates/widget_audit.py"] == tmpl_sha
    assert manifest["name"] == "toy-widgets"
    assert manifest["seams"] == {"audit_template": "templates/widget_audit.py"}


def test_serialize_is_sorted_indent2_trailing_newline(tmp_path: Path) -> None:
    pack_root = tmp_path / "pack"
    _write_pack(pack_root)
    recipe = pack_sweep.load_recipe(pack_root / "sweep.json")
    text = pack_sweep.serialize_manifest(pack_sweep.fresh_manifest_dict(recipe, pack_root))
    assert text.endswith("\n")
    # Byte-identical to the pack build-script form: json.dumps(indent=2, sort_keys).
    reparsed = json.loads(text)
    assert text == json.dumps(reparsed, indent=2, sort_keys=True) + "\n"


# ── staleness (SEMANTIC) ─────────────────────────────────────────────────────


def test_not_stale_when_manifest_current(tmp_path: Path) -> None:
    pack_root = tmp_path / "pack"
    _write_pack(pack_root)
    manifest_path = pack_root / "manifest.json"
    # Seal once.
    first = pack_sweep.reseal_manifest(manifest_path, pack_root / "sweep.json")
    assert first.stale is True and first.wrote is True
    # A second reseal over unchanged content finds nothing stale — byte no-op.
    again = pack_sweep.reseal_manifest(manifest_path, pack_root / "sweep.json")
    assert again.stale is False and again.wrote is False
    assert again.old_manifest_sha == again.new_manifest_sha


def test_whitespace_churn_is_not_stale(tmp_path: Path) -> None:
    pack_root = tmp_path / "pack"
    _write_pack(pack_root)
    manifest_path = pack_root / "manifest.json"
    pack_sweep.reseal_manifest(manifest_path, pack_root / "sweep.json")
    # Reformat the manifest with different whitespace but identical content.
    reparsed = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_path.write_text(json.dumps(reparsed) + "   ", encoding="utf-8")
    out = pack_sweep.reseal_manifest(manifest_path, pack_root / "sweep.json")
    assert out.stale is False  # semantic, not byte-exact


def test_edited_file_is_stale_and_reseals(tmp_path: Path) -> None:
    pack_root = tmp_path / "pack"
    _write_pack(pack_root)
    manifest_path = pack_root / "manifest.json"
    pack_sweep.reseal_manifest(manifest_path, pack_root / "sweep.json")
    old_sha = _raw_sha(manifest_path.read_bytes())
    # Edit a sealed file → the manifest becomes stale.
    (pack_root / "templates" / "widget_audit.py").write_text("# %% audit v2\n", encoding="utf-8")
    out = pack_sweep.reseal_manifest(manifest_path, pack_root / "sweep.json")
    assert out.stale is True and out.wrote is True
    assert out.old_manifest_sha != out.new_manifest_sha
    assert "templates/widget_audit.py" in out.changed_files
    assert out.old_manifest_sha == old_sha


def test_minimal_set_editing_one_pack_leaves_other_current(tmp_path: Path) -> None:
    """Editing pack A's content never marks pack B's manifest stale."""
    a = tmp_path / "packA"
    b = tmp_path / "packB"
    _write_pack(a)
    _write_pack(b)
    pack_sweep.reseal_manifest(a / "manifest.json", a / "sweep.json")
    pack_sweep.reseal_manifest(b / "manifest.json", b / "sweep.json")
    # Edit only A.
    (a / "templates" / "widget_audit.py").write_text("# changed\n", encoding="utf-8")
    assert pack_sweep.reseal_manifest(a / "manifest.json", a / "sweep.json").stale is True
    assert pack_sweep.reseal_manifest(b / "manifest.json", b / "sweep.json").stale is False


def test_vanished_sealed_file_is_loud(tmp_path: Path) -> None:
    pack_root = tmp_path / "pack"
    _write_pack(pack_root)
    (pack_root / "templates" / "widget_audit.py").unlink()
    with pytest.raises(errors.SpecInvalid, match="unreadable|vanished"):
        pack_sweep.reseal_manifest(pack_root / "manifest.json", pack_root / "sweep.json")
