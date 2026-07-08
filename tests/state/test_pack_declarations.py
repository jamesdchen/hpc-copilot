"""Tests for the ONE domain-pack seam-declaration resolver (T7).

Covers: the D7 silence (absent opt-in → empty + ZERO probes beyond
interview.json), the happy path (current bind + clean files → typed declarations
each carrying the ``{pack, version, sha}`` echo), the loud dangling/drift posture
(missing manifest, no current bind, on-disk drift of a seam file, a re-generated
manifest), per-seam shape round-trips, and the DP2/DP3 no-import/exec AST pin.

The T5 record verb (``pack-record-receipt``) and the T8 ``"pack"`` journal scope
are later waves, so the pack journal records are crafted as TOY dicts in the
append_decision shape and passed via ``records_by_pack`` — the resolver takes the
records at the boundary (the ``# T8 seam:`` note), never reading a journal itself.
Toy-domain vocabulary only (``toy-widgets``/``widgets.load_widget``) — never a
real domain's words.
"""

from __future__ import annotations

import ast
import hashlib
import json
from typing import TYPE_CHECKING, Any

import pytest

import hpc_agent.state.pack as pack
import hpc_agent.state.pack_declarations as pd
import hpc_agent.state.pack_receipts as pr
from hpc_agent import errors

if TYPE_CHECKING:
    from pathlib import Path

_PACK = "toy-widgets"
_MANIFEST_REL = "packs/toy/manifest.json"
_AXIS = next(iter(sorted(pack.AXIS_LITERALS)))

_SEAM_FILES: dict[str, tuple[str, Any]] = {
    "reader_calls": ("readers.json", ["widgets.load_widget", "widgets.load_frame"]),
    "failure_patterns": ("failures.json", {"widget-jam": r"widget jam at \d+"}),
    "axis_hints": ("hints.json", [{"pattern": r"^seed", "axis": _AXIS}]),
    "tolerances": ("tols.json", {"widget-rmse": 0.01}),
    "registration_fields": ("fields.json", ["widget-owner"]),
}


def _raw_sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _build_pack(experiment_dir: Path) -> tuple[str, dict[str, Any]]:
    """Write a toy pack (every loadable seam) under *experiment_dir*; return
    ``(manifest_sha, manifest_dict)``."""
    pack_root = experiment_dir / "packs" / "toy"
    pack_root.mkdir(parents=True, exist_ok=True)
    files = []
    seams = {}
    for seam, (rel, payload) in _SEAM_FILES.items():
        blob = json.dumps(payload).encode("utf-8")
        (pack_root / rel).write_bytes(blob)
        files.append({"path": rel, "sha256": _raw_sha(blob)})
        seams[seam] = rel
    manifest = {
        "name": _PACK,
        "version": "1.2.0",
        "files": files,
        "seams": seams,
        "fills_slots": ["widget-audit"],
    }
    manifest_blob = json.dumps(manifest).encode("utf-8")
    (pack_root / "manifest.json").write_bytes(manifest_blob)
    return _raw_sha(manifest_blob), manifest


def _bind_record(manifest: dict[str, Any], manifest_sha: str) -> dict[str, Any]:
    return {
        "block": pr.PACK_BIND_BLOCK,
        "resolved": {
            "pack": _PACK,
            "version": manifest["version"],
            "manifest_sha": manifest_sha,
            "files": manifest["files"],
            "seams": list(manifest["seams"]),
        },
    }


def _write_interview(experiment_dir: Path, packs_block: Any) -> None:
    doc = {"packs": packs_block} if packs_block is not _ABSENT else {}
    (experiment_dir / "interview.json").write_text(json.dumps(doc), encoding="utf-8")


_ABSENT = object()


def _opted_in(experiment_dir: Path) -> dict[str, Any]:
    """Full happy-path setup: pack on disk + a current bind + an opt-in block.
    Returns ``records_by_pack``."""
    manifest_sha, manifest = _build_pack(experiment_dir)
    _write_interview(
        experiment_dir,
        [{"pack": _PACK, "manifest": _MANIFEST_REL, "receipt_bindings": []}],
    )
    return {_PACK: [_bind_record(manifest, manifest_sha)]}


# ── D7 silence: absent opt-in → empty + zero probes ──────────────────────────


def test_absent_optin_is_empty_and_probes_nothing(tmp_path: Path, monkeypatch: Any) -> None:
    # No interview.json at all → not opted in.
    def _boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("resolver probed the filesystem beyond interview.json")

    monkeypatch.setattr(pd, "load_manifest", _boom)
    monkeypatch.setattr(pd, "current_bind", _boom)
    monkeypatch.setattr(pd, "_read_json_file", _boom)

    out = pd.resolve_declarations(tmp_path, records_by_pack={})
    assert out.reader_calls == ()
    assert out.failure_patterns == ()
    assert out.axis_hints == ()
    assert out.tolerances == ()
    assert out.registration_fields == ()
    assert pd.resolve_reader_calls(tmp_path, records_by_pack={}) == []


def test_interview_without_packs_key_is_empty(tmp_path: Path) -> None:
    (tmp_path / "interview.json").write_text(json.dumps({"goal": "x"}), encoding="utf-8")
    assert pd.resolve_declarations(tmp_path).reader_calls == ()


# ── happy path: current bind + clean files → declarations with echoes ────────


def test_resolve_declarations_happy_path(tmp_path: Path) -> None:
    records = _opted_in(tmp_path)
    out = pd.resolve_declarations(tmp_path, records_by_pack=records)

    assert len(out.reader_calls) == 1
    rc = out.reader_calls[0]
    assert rc.names == ("widgets.load_widget", "widgets.load_frame")
    assert rc.echo.pack == _PACK
    assert rc.echo.version == "1.2.0"
    assert rc.echo.sha and len(rc.echo.sha) == 64
    assert rc.echo.as_dict() == {"pack": _PACK, "version": "1.2.0", "sha": rc.echo.sha}

    assert out.failure_patterns[0].patterns == {"widget-jam": r"widget jam at \d+"}
    assert out.axis_hints[0].hints == ({"pattern": r"^seed", "axis": _AXIS},)
    assert out.tolerances[0].tolerances == {"widget-rmse": 0.01}
    assert out.registration_fields[0].fields == ("widget-owner",)
    # Every seam's echo is the same bind identity.
    for decl in (out.failure_patterns[0], out.axis_hints[0], out.tolerances[0]):
        assert decl.echo.sha == rc.echo.sha


# ── per-seam accessor round-trips ────────────────────────────────────────────


def test_per_seam_accessors_round_trip(tmp_path: Path) -> None:
    records = _opted_in(tmp_path)
    assert pd.resolve_reader_calls(tmp_path, records_by_pack=records)[0].names == (
        "widgets.load_widget",
        "widgets.load_frame",
    )
    assert pd.resolve_failure_patterns(tmp_path, records_by_pack=records)[0].patterns == {
        "widget-jam": r"widget jam at \d+"
    }
    assert pd.resolve_axis_hints(tmp_path, records_by_pack=records)[0].hints == (
        {"pattern": r"^seed", "axis": _AXIS},
    )
    assert pd.resolve_tolerances(tmp_path, records_by_pack=records)[0].tolerances == {
        "widget-rmse": 0.01
    }
    assert pd.resolve_registration_fields(tmp_path, records_by_pack=records)[0].fields == (
        "widget-owner",
    )


def test_records_reader_seam(tmp_path: Path) -> None:
    # The # T8 seam: a reader callable instead of a mapping.
    records = _opted_in(tmp_path)
    out = pd.resolve_reader_calls(tmp_path, records_reader=lambda name: records.get(name, ()))
    assert out[0].echo.pack == _PACK


# ── loud dangling / drift posture ────────────────────────────────────────────


def test_missing_manifest_is_loud(tmp_path: Path) -> None:
    _write_interview(
        tmp_path, [{"pack": _PACK, "manifest": "packs/gone/manifest.json", "receipt_bindings": []}]
    )
    with pytest.raises(errors.SpecInvalid, match="manifest"):
        pd.resolve_declarations(tmp_path, records_by_pack={_PACK: []})


def test_no_current_bind_is_loud(tmp_path: Path) -> None:
    _build_pack(tmp_path)
    _write_interview(tmp_path, [{"pack": _PACK, "manifest": _MANIFEST_REL, "receipt_bindings": []}])
    with pytest.raises(errors.SpecInvalid, match="no CURRENT bind"):
        pd.resolve_declarations(tmp_path, records_by_pack={_PACK: []})


def test_seam_file_drift_is_loud(tmp_path: Path) -> None:
    records = _opted_in(tmp_path)
    # Edit a bound seam file on disk after the bind → its raw sha moves.
    (tmp_path / "packs" / "toy" / "readers.json").write_bytes(b'["widgets.tampered"]')
    with pytest.raises(errors.SpecInvalid, match="sha mismatch|drift"):
        pd.resolve_declarations(tmp_path, records_by_pack=records)


def test_manifest_regenerated_without_rebind_is_loud(tmp_path: Path) -> None:
    records = _opted_in(tmp_path)
    # Rewrite the manifest bytes (e.g. re-generated) without re-binding → its raw
    # sha no longer equals the bind's manifest_sha → drift, even though every
    # listed file still matches.
    mpath = tmp_path / "packs" / "toy" / "manifest.json"
    doc = json.loads(mpath.read_text(encoding="utf-8"))
    doc["fills_slots"] = ["widget-audit", "extra-slot"]  # a byte change, still valid shape
    mpath.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(errors.SpecInvalid, match="no longer matches the current bind"):
        pd.resolve_declarations(tmp_path, records_by_pack=records)


def test_pack_name_mismatch_is_loud(tmp_path: Path) -> None:
    records = _opted_in(tmp_path)
    # Opt-in names a different pack than the manifest declares → dangling.
    _write_interview(
        tmp_path, [{"pack": "other-pack", "manifest": _MANIFEST_REL, "receipt_bindings": []}]
    )
    with pytest.raises(errors.SpecInvalid, match="dangling/mismatched"):
        pd.resolve_declarations(tmp_path, records_by_pack={"other-pack": records[_PACK]})


def test_malformed_packs_block_is_loud(tmp_path: Path) -> None:
    (tmp_path / "interview.json").write_text(json.dumps({"packs": "nope"}), encoding="utf-8")
    with pytest.raises(errors.SpecInvalid, match="must be a list"):
        pd.resolve_declarations(tmp_path)


def test_entry_without_manifest_is_loud(tmp_path: Path) -> None:
    _write_interview(tmp_path, [{"pack": _PACK}])
    with pytest.raises(errors.SpecInvalid, match="manifest"):
        pd.resolve_declarations(tmp_path, records_by_pack={_PACK: []})


# ── partial declarations: a pack silent on a seam contributes nothing ────────


def test_pack_declaring_only_one_seam(tmp_path: Path) -> None:
    pack_root = tmp_path / "packs" / "toy"
    pack_root.mkdir(parents=True)
    blob = json.dumps(["widgets.load_widget"]).encode("utf-8")
    (pack_root / "readers.json").write_bytes(blob)
    manifest = {
        "name": _PACK,
        "version": "1.0.0",
        "files": [{"path": "readers.json", "sha256": _raw_sha(blob)}],
        "seams": {"reader_calls": "readers.json"},
        "fills_slots": [],
    }
    mblob = json.dumps(manifest).encode("utf-8")
    (pack_root / "manifest.json").write_bytes(mblob)
    _write_interview(tmp_path, [{"pack": _PACK, "manifest": _MANIFEST_REL, "receipt_bindings": []}])
    out = pd.resolve_declarations(
        tmp_path, records_by_pack={_PACK: [_bind_record(manifest, _raw_sha(mblob))]}
    )
    assert len(out.reader_calls) == 1
    assert out.failure_patterns == ()
    assert out.tolerances == ()


# ── DP2/DP3 AST pin: core never imports or executes pack content ─────────────


def test_module_never_imports_or_executes_pack_content() -> None:
    """No ``importlib`` / ``entry_points`` / ``exec`` / ``eval`` in the resolver.

    DP3 (distribution invisible) + DP2 (pack code never runs in core): the
    resolver reads bytes and reduces shape — it must never gain an import-or-
    execute path over pack-named content.
    """
    from pathlib import Path as _Path

    source = _Path(pd.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=pd.__file__)
    forbidden_names = {"exec", "eval"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] != "importlib", "no importlib in the resolver"
        if isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            assert root != "importlib", "no importlib in the resolver"
        if isinstance(node, ast.Attribute):
            assert node.attr != "entry_points", "no entry_points in the resolver"
        if isinstance(node, ast.Name):
            assert node.id not in forbidden_names, f"no {node.id} in the resolver"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
