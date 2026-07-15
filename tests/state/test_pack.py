"""Shape-only contract for the domain-pack manifest model + seam loaders.

Toy-widgets vocabulary ONLY (``widgets.load_widget``, ``widget-jam``) — never a
real domain's words, so a grep of this tree never mistakes a fixture for core
knowledge. Every refusal path fires on a synthetic violation (the
``test_lint_rule_fires_on_synthetic_input`` doctrine); the loaders round-trip;
``SEAM_NAMES`` is equality-pinned; and an AST pin proves the module never
imports or executes pack content (DP2/DP3).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent.experiment_kit.axis import DataAxis
from hpc_agent.state import pack

_MODULE_FILE = Path(pack.__file__)


# --- the closed vocabularies ------------------------------------------------


def test_seam_names_equality_pin() -> None:
    """``SEAM_NAMES`` equals the agreed closed set exactly (the DOSSIER_SOURCES pin)."""
    assert (
        frozenset(
            {
                "reader_calls",
                "failure_patterns",
                "axis_hints",
                "audit_template",
                "tolerances",
                "registration_fields",
            }
        )
        == pack.SEAM_NAMES
    )
    # The reserved future member must NOT be present yet.
    assert "actor_policy" not in pack.SEAM_NAMES
    # ``required_receipts`` is an S6 sibling loader, NOT a seam name.
    assert "required_receipts" not in pack.SEAM_NAMES


def test_axis_literals_are_core_dataaxis_by_identity() -> None:
    """``AXIS_LITERALS`` is derived from core's existing ``DataAxis`` union — no new vocab."""
    import typing

    expected = {t.__name__ for t in typing.get_args(DataAxis)}
    assert frozenset(expected) == pack.AXIS_LITERALS
    assert (
        frozenset({"Independent", "Associative", "BoundedHalo", "Sequential"}) == pack.AXIS_LITERALS
    )


# --- sha helpers ------------------------------------------------------------


def test_sha256_helpers_are_raw_bytes_lowercase_hex(tmp_path: Path) -> None:
    payload = b"widget bytes"
    import hashlib

    expected = hashlib.sha256(payload).hexdigest()
    assert pack.sha256_bytes(payload) == expected
    f = tmp_path / "readers.json"
    f.write_bytes(payload)
    assert pack.sha256_file(f) == expected


# --- manifest round-trip ----------------------------------------------------


def _toy_manifest_dict(files: list[dict[str, str]], seams: dict[str, str]) -> dict[str, object]:
    return {
        "name": "toy-widgets",
        "version": "1.2.0",
        "files": files,
        "seams": seams,
        "fills_slots": ["widget-audit"],
    }


def _write_pack(tmp_path: Path) -> tuple[Path, pack.PackManifest]:
    """Write a coherent toy pack on disk; return (pack_root, parsed manifest)."""
    readers = json.dumps(["widgets.load_widget", "pandas.read_csv"]).encode("utf-8")
    failures = json.dumps({"widget-jam": r"jam at slot \d+"}).encode("utf-8")
    (tmp_path / "vocab").mkdir()
    (tmp_path / "patterns").mkdir()
    (tmp_path / "vocab" / "readers.json").write_bytes(readers)
    (tmp_path / "patterns" / "failures.json").write_bytes(failures)
    manifest = _toy_manifest_dict(
        files=[
            {"path": "vocab/readers.json", "sha256": pack.sha256_bytes(readers)},
            {"path": "patterns/failures.json", "sha256": pack.sha256_bytes(failures)},
        ],
        seams={
            "reader_calls": "vocab/readers.json",
            "failure_patterns": "patterns/failures.json",
        },
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path, pack.load_manifest(manifest_path)


def test_manifest_round_trip(tmp_path: Path) -> None:
    pack_root, m = _write_pack(tmp_path)
    assert m.name == "toy-widgets"
    assert m.version == "1.2.0"
    assert {f.path for f in m.files} == {"vocab/readers.json", "patterns/failures.json"}
    assert m.seams["reader_calls"] == "vocab/readers.json"
    assert m.fills_slots == ("widget-audit",)
    assert m.sha_for("vocab/readers.json") is not None
    assert m.sha_for("nope") is None
    # Integrity passes against the bytes on disk.
    pack.verify_manifest_integrity(pack_root, m)


# --- integrity refusals -----------------------------------------------------


def test_integrity_refuses_sha_mismatch(tmp_path: Path) -> None:
    pack_root, m = _write_pack(tmp_path)
    (pack_root / "vocab" / "readers.json").write_bytes(b"edited without re-binding")
    with pytest.raises(errors.SpecInvalid) as exc:
        pack.verify_manifest_integrity(pack_root, m)
    assert "vocab/readers.json" in str(exc.value)


def test_integrity_refuses_missing_file(tmp_path: Path) -> None:
    pack_root, m = _write_pack(tmp_path)
    (pack_root / "patterns" / "failures.json").unlink()
    with pytest.raises(errors.SpecInvalid) as exc:
        pack.verify_manifest_integrity(pack_root, m)
    assert "patterns/failures.json" in str(exc.value)


def test_load_manifest_refuses_missing(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid):
        pack.load_manifest(tmp_path / "nope.json")


def test_load_manifest_refuses_non_json(tmp_path: Path) -> None:
    p = tmp_path / "manifest.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(errors.SpecInvalid):
        pack.load_manifest(p)


# --- parse refusals ---------------------------------------------------------


def test_parse_refuses_non_slug_name() -> None:
    data = _toy_manifest_dict(files=[], seams={})
    data["name"] = "bad name with spaces"
    with pytest.raises(errors.SpecInvalid):
        pack.parse_manifest(data)


def test_parse_refuses_empty_version() -> None:
    data = _toy_manifest_dict(files=[], seams={})
    data["version"] = ""
    with pytest.raises(errors.SpecInvalid):
        pack.parse_manifest(data)


def test_parse_refuses_bad_sha_shape() -> None:
    data = _toy_manifest_dict(
        files=[{"path": "vocab/readers.json", "sha256": "TOOSHORT"}],
        seams={},
    )
    with pytest.raises(errors.SpecInvalid) as exc:
        pack.parse_manifest(data)
    assert "sha256" in str(exc.value)


def test_parse_refuses_unknown_seam_name() -> None:
    sha = "a" * 64
    data = _toy_manifest_dict(
        files=[{"path": "vocab/readers.json", "sha256": sha}],
        seams={"bogus_seam": "vocab/readers.json"},
    )
    with pytest.raises(errors.SpecInvalid) as exc:
        pack.parse_manifest(data)
    assert "unknown seam" in str(exc.value)


def test_parse_refuses_unlisted_seam_pointer() -> None:
    sha = "a" * 64
    data = _toy_manifest_dict(
        files=[{"path": "vocab/readers.json", "sha256": sha}],
        seams={"failure_patterns": "patterns/not-listed.json"},
    )
    with pytest.raises(errors.SpecInvalid) as exc:
        pack.parse_manifest(data)
    assert "not a listed file" in str(exc.value)


def test_parse_refuses_duplicate_file_path() -> None:
    sha = "a" * 64
    data = _toy_manifest_dict(
        files=[
            {"path": "vocab/readers.json", "sha256": sha},
            {"path": "vocab/readers.json", "sha256": sha},
        ],
        seams={},
    )
    with pytest.raises(errors.SpecInvalid) as exc:
        pack.parse_manifest(data)
    assert "duplicate" in str(exc.value)


def test_parse_refuses_non_slug_fills_slot() -> None:
    data = _toy_manifest_dict(files=[], seams={})
    data["fills_slots"] = ["bad slot"]
    with pytest.raises(errors.SpecInvalid):
        pack.parse_manifest(data)


# --- seam-loader round-trips ------------------------------------------------


def test_reader_calls_round_trip() -> None:
    out = pack.load_reader_calls(["widgets.load_widget", "pandas.read_csv"], source="readers.json")
    assert out == ["widgets.load_widget", "pandas.read_csv"]


def test_failure_patterns_round_trip() -> None:
    out = pack.load_failure_patterns({"widget-jam": r"jam at \d+"}, source="failures.json")
    assert out == {"widget-jam": r"jam at \d+"}


def test_axis_hints_round_trip() -> None:
    out = pack.load_axis_hints(
        [{"pattern": r"^widget_seed$", "axis": "Independent"}], source="hints.json"
    )
    assert out == [{"pattern": r"^widget_seed$", "axis": "Independent"}]


def test_tolerances_round_trip() -> None:
    out = pack.load_tolerances({"widget-rtol": 0.001, "widget-atol": 1}, source="tol.json")
    assert out == {"widget-rtol": 0.001, "widget-atol": 1}


def test_registration_and_required_receipts_round_trip() -> None:
    assert pack.load_registration_fields(["widget-owner"], source="reg.json") == ["widget-owner"]
    assert pack.load_required_receipts(["widget-audit"], source="req.json") == ["widget-audit"]


def test_dispatch_round_trips_content_seams() -> None:
    assert pack.load_seam_declaration("reader_calls", ["widgets.load_widget"], source="r.json") == [
        "widgets.load_widget"
    ]


# --- seam-loader refusals ---------------------------------------------------


def test_reader_calls_refuses_non_list() -> None:
    with pytest.raises(errors.SpecInvalid):
        pack.load_reader_calls({"widgets.load_widget": 1}, source="readers.json")


def test_failure_patterns_refuses_non_compiling_regex() -> None:
    with pytest.raises(errors.SpecInvalid) as exc:
        pack.load_failure_patterns({"widget-jam": "unterminated ["}, source="failures.json")
    assert "regex" in str(exc.value)


def test_failure_patterns_refuses_non_slug_id() -> None:
    with pytest.raises(errors.SpecInvalid):
        pack.load_failure_patterns({"bad id": r"\d+"}, source="failures.json")


def test_axis_hints_refuses_unknown_axis_literal() -> None:
    with pytest.raises(errors.SpecInvalid) as exc:
        pack.load_axis_hints([{"pattern": r"x", "axis": "Diagonal"}], source="hints.json")
    assert "DataAxis" in str(exc.value)


def test_axis_hints_refuses_non_compiling_pattern() -> None:
    with pytest.raises(errors.SpecInvalid):
        pack.load_axis_hints([{"pattern": "bad [", "axis": "Independent"}], source="hints.json")


def test_axis_hints_refuses_extra_keys() -> None:
    with pytest.raises(errors.SpecInvalid) as exc:
        pack.load_axis_hints(
            [{"pattern": r"x", "axis": "Independent", "meaning": "seed"}], source="hints.json"
        )
    assert "unexpected keys" in str(exc.value)


def test_tolerances_refuses_non_number() -> None:
    with pytest.raises(errors.SpecInvalid):
        pack.load_tolerances({"widget-rtol": "0.1"}, source="tol.json")


def test_tolerances_refuses_bool() -> None:
    with pytest.raises(errors.SpecInvalid):
        pack.load_tolerances({"widget-rtol": True}, source="tol.json")


def test_registration_fields_refuses_non_slug() -> None:
    with pytest.raises(errors.SpecInvalid):
        pack.load_registration_fields(["bad field"], source="reg.json")


def test_dispatch_refuses_audit_template() -> None:
    with pytest.raises(errors.SpecInvalid) as exc:
        pack.load_seam_declaration("audit_template", [], source="t.py")
    assert "audit_template" in str(exc.value)


def test_dispatch_refuses_unknown_seam() -> None:
    with pytest.raises(errors.SpecInvalid):
        pack.load_seam_declaration("bogus", [], source="x.json")


# --- DP2/DP3 AST pin: core never imports or executes pack content -----------


def test_module_never_imports_or_executes_pack_content() -> None:
    """No ``importlib`` / ``entry_points`` / ``exec`` / ``eval`` in the module.

    DP3 (distribution invisible) + DP2 (code never runs in core): the pack
    substrate hashes bytes and validates shape — it must never gain an
    import-or-execute path over pack-named content. T11 mirrors this into the
    contract suite.
    """
    tree = ast.parse(_MODULE_FILE.read_text(encoding="utf-8"), filename=str(_MODULE_FILE))
    forbidden_names = {"exec", "eval"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] != "importlib", "no importlib in the pack substrate"
        if isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            assert root != "importlib", "no importlib in the pack substrate"
        if isinstance(node, ast.Attribute):
            assert node.attr != "entry_points", "no entry_points in the pack substrate"
        if isinstance(node, ast.Name):
            assert node.id not in forbidden_names, f"no {node.id} in the pack substrate"


# --- derived_from lineage (P1a) ---------------------------------------------


def _good_derived_from() -> dict[str, str]:
    return {
        "pack": "toy-widgets",
        "seam": "audit_template",
        "version": "1.0.0",
        "sha": "a" * 64,
    }


def test_manifest_without_derived_from_is_back_compat() -> None:
    """A legacy manifest (no ``derived_from`` key) parses to ``None`` — unchanged."""
    m = pack.parse_manifest(
        {"name": "toy-widgets", "version": "1", "files": [], "seams": {}, "fills_slots": []}
    )
    assert m.derived_from is None


def test_well_formed_derived_from_parses_into_frozen_dataclass() -> None:
    """A well-formed block parses into the frozen :class:`DerivedFrom` on the manifest."""
    m = pack.parse_manifest(
        {
            "name": "toy-widgets",
            "version": "1",
            "files": [],
            "seams": {},
            "fills_slots": [],
            "derived_from": _good_derived_from(),
        }
    )
    assert m.derived_from == pack.DerivedFrom(
        pack="toy-widgets", seam="audit_template", version="1.0.0", sha="a" * 64
    )
    # Frozen — a lineage stamp is immutable identity.
    with pytest.raises(Exception):  # noqa: B017 — FrozenInstanceError
        m.derived_from.sha = "b" * 64  # type: ignore[misc]


def test_parse_derived_from_round_trips() -> None:
    """The shared :func:`parse_derived_from` accepts a well-formed block."""
    df = pack.parse_derived_from(_good_derived_from(), what="test")
    assert (df.pack, df.seam, df.version, df.sha) == (
        "toy-widgets",
        "audit_template",
        "1.0.0",
        "a" * 64,
    )


def test_derived_from_refusals_fire_on_synthetic_violations() -> None:
    """Every derived_from shape refusal FIRES (the fires-AND-passes doctrine)."""
    # non-object block
    with pytest.raises(errors.SpecInvalid):
        pack.parse_derived_from(["not", "an", "object"], what="test")
    # non-slug pack
    bad_pack = _good_derived_from() | {"pack": "bad pack!"}
    with pytest.raises(errors.SpecInvalid):
        pack.parse_derived_from(bad_pack, what="test")
    # seam outside SEAM_NAMES
    bad_seam = _good_derived_from() | {"seam": "not_a_seam"}
    with pytest.raises(errors.SpecInvalid, match="seam"):
        pack.parse_derived_from(bad_seam, what="test")
    # empty version
    bad_ver = _good_derived_from() | {"version": ""}
    with pytest.raises(errors.SpecInvalid, match="version"):
        pack.parse_derived_from(bad_ver, what="test")
    # non-64-hex sha
    bad_sha = _good_derived_from() | {"sha": "deadbeef"}
    with pytest.raises(errors.SpecInvalid, match="sha"):
        pack.parse_derived_from(bad_sha, what="test")
    # a malformed derived_from inside a manifest is loud, never a silent drop
    with pytest.raises(errors.SpecInvalid):
        pack.parse_manifest(
            {
                "name": "toy-widgets",
                "version": "1",
                "files": [],
                "seams": {},
                "fills_slots": [],
                "derived_from": {"pack": "toy-widgets"},  # missing seam/version/sha
            }
        )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
