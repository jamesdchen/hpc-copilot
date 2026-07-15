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


# ── derived_from lineage (P1a) ───────────────────────────────────────────────

_DERIVED = {
    "pack": "widget-domain",
    "seam": "audit_template",
    "version": "1.0.0",
    "sha": "a" * 64,
}


def _write_pack_with_derived(pack_root: Path) -> None:
    """A toy program pack whose recipe carries a ``derived_from`` block."""
    pack_root.mkdir(parents=True, exist_ok=True)
    (pack_root / "templates").mkdir(exist_ok=True)
    (pack_root / "templates" / "widget_audit.py").write_text("# %% audit\n", encoding="utf-8")
    recipe = {
        "name": "toy-program",
        "version": "1.0.0",
        "seams": {"audit_template": "templates/widget_audit.py"},
        "fills_slots": [],
        "pack_files": ["templates/widget_audit.py"],
        "sweep": [],
        "derived_from": _DERIVED,
    }
    (pack_root / "sweep.json").write_text(json.dumps(recipe, indent=2), encoding="utf-8")


def test_recipe_derived_from_round_trips_into_manifest(tmp_path: Path) -> None:
    """A recipe ``derived_from`` flows verbatim into the rebuilt manifest, canonically."""
    pack_root = tmp_path / "pack"
    _write_pack_with_derived(pack_root)
    recipe = pack_sweep.load_recipe(pack_root / "sweep.json")
    assert recipe.derived_from is not None
    manifest = pack_sweep.fresh_manifest_dict(recipe, pack_root)
    assert manifest["derived_from"] == _DERIVED
    # canonical bytes stable under sort_keys
    text = pack_sweep.serialize_manifest(manifest)
    assert text == json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n"


def test_semantic_absent_in_both_is_not_stale(tmp_path: Path) -> None:
    """REGRESSION (memo hazard 1): a legacy manifest+recipe (no derived_from) is NOT stale.

    Getting this wrong mass-reseals every live pack the moment a fix wheel ships,
    revoking their receipts. Absent-in-both must compare ``_semantic``-equal
    between recipe-fresh and on-disk.
    """
    pack_root = tmp_path / "pack"
    _write_pack(pack_root)  # NO derived_from
    manifest_path = pack_root / "manifest.json"
    pack_sweep.reseal_manifest(manifest_path, pack_root / "sweep.json")
    on_disk = json.loads(manifest_path.read_text(encoding="utf-8"))
    fresh = pack_sweep.fresh_manifest_dict(
        pack_sweep.load_recipe(pack_root / "sweep.json"), pack_root
    )
    assert "derived_from" not in on_disk and "derived_from" not in fresh
    assert pack_sweep._semantic(on_disk) == pack_sweep._semantic(fresh)
    # And the whole reseal is a no-op — no spurious staleness.
    assert pack_sweep.reseal_manifest(manifest_path, pack_root / "sweep.json").stale is False


def test_hand_edited_manifest_derived_from_reads_stale_and_reseals_from_recipe(
    tmp_path: Path,
) -> None:
    """A hand-edited manifest ``derived_from`` self-revokes — the recipe is truth (DC3)."""
    pack_root = tmp_path / "pack"
    _write_pack_with_derived(pack_root)
    manifest_path = pack_root / "manifest.json"
    pack_sweep.reseal_manifest(manifest_path, pack_root / "sweep.json")
    # Hand-edit the manifest's derived_from to a different sha.
    doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    doc["derived_from"]["sha"] = "b" * 64
    manifest_path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    out = pack_sweep.reseal_manifest(manifest_path, pack_root / "sweep.json")
    assert out.stale is True and out.wrote is True
    # Reseal restored the RECIPE's value (not the hand-edit).
    restored = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert restored["derived_from"] == _DERIVED


def test_manifest_with_recipe_without_reads_stale(tmp_path: Path) -> None:
    """Recipe-without + manifest-with ``derived_from`` → stale (reseal drops it; DC3)."""
    pack_root = tmp_path / "pack"
    _write_pack(pack_root)  # recipe has NO derived_from
    manifest_path = pack_root / "manifest.json"
    pack_sweep.reseal_manifest(manifest_path, pack_root / "sweep.json")
    # Inject a derived_from into the manifest only.
    doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    doc["derived_from"] = _DERIVED
    manifest_path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    out = pack_sweep.reseal_manifest(manifest_path, pack_root / "sweep.json")
    assert out.stale is True and out.wrote is True
    # The recipe is truth: the reseal DROPPED the manifest-only stamp.
    assert "derived_from" not in json.loads(manifest_path.read_text(encoding="utf-8"))


# ── stamp_recipe_derived_from (adopt migration path) ─────────────────────────


def test_stamp_recipe_preserves_unknown_keys(tmp_path: Path) -> None:
    """The raw read-modify-write stamp preserves an unknown lab key (premortem)."""
    from hpc_agent.state.pack import DerivedFrom

    pack_root = tmp_path / "pack"
    _write_pack(pack_root)
    recipe_path = pack_root / "sweep.json"
    # A lab added an unknown key that load_recipe would silently drop on round-trip.
    doc = json.loads(recipe_path.read_text(encoding="utf-8"))
    doc["lab_extra"] = "KEEP_ME"
    recipe_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")

    pack_sweep.stamp_recipe_derived_from(
        recipe_path,
        DerivedFrom(pack="widget-domain", seam="audit_template", version="1.0.0", sha="a" * 64),
    )
    after = json.loads(recipe_path.read_text(encoding="utf-8"))
    assert after["lab_extra"] == "KEEP_ME"  # unknown key survived
    assert after["derived_from"] == _DERIVED
    # And load_recipe now reads the stamp.
    assert pack_sweep.load_recipe(recipe_path).derived_from is not None


def test_stamp_recipe_missing_is_loud(tmp_path: Path) -> None:
    from hpc_agent.state.pack import DerivedFrom

    with pytest.raises(errors.SpecInvalid):
        pack_sweep.stamp_recipe_derived_from(
            tmp_path / "nope.json",
            DerivedFrom(pack="p", seam="audit_template", version="1", sha="a" * 64),
        )
