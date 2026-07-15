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
import dataclasses
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


# ── S4 template-pack echo (T9d): FAIL-OPEN file-identity lookup ──────────────


def _bind_sha(records: dict[str, Any]) -> str:
    """The manifest sha the crafted bind record recorded (for echo assertions)."""
    return str(records[_PACK][0]["resolved"]["manifest_sha"])


def test_template_echo_when_template_is_a_bound_pack_file(tmp_path: Path) -> None:
    # A file listed in the CURRENT bind's manifest → the {pack, version, sha} echo.
    # Match is file-path IDENTITY (any manifest file), not a name/extension check.
    records = _opted_in(tmp_path)
    echo = pd.resolve_template_pack_echo(tmp_path, "packs/toy/hints.json", records_by_pack=records)
    assert echo == {"pack": _PACK, "version": "1.2.0", "sha": _bind_sha(records)}


def test_template_echo_none_when_template_not_in_files(tmp_path: Path) -> None:
    records = _opted_in(tmp_path)
    assert (
        pd.resolve_template_pack_echo(
            tmp_path, "packs/toy/not_a_pack_file.py", records_by_pack=records
        )
        is None
    )


def test_template_echo_none_when_not_opted_in(tmp_path: Path) -> None:
    # No interview.json → silent-absent, zero probes (the D7 silence on the echo).
    assert pd.resolve_template_pack_echo(tmp_path, "packs/toy/hints.json") is None


def test_template_echo_fail_open_on_dangling_bind(tmp_path: Path) -> None:
    # Opted in but NO current bind (empty records) → fail-open None, never the loud
    # dangling refusal (that stays on the enforcement resolvers).
    _opted_in(tmp_path)
    assert (
        pd.resolve_template_pack_echo(tmp_path, "packs/toy/hints.json", records_by_pack={}) is None
    )


def test_template_echo_fail_open_on_manifest_drift(tmp_path: Path) -> None:
    # Re-generate the manifest on disk AFTER the bind → its sha no longer matches
    # the current bind → no honest echo (fail-open None), the drift-revocation the
    # design earns, expressed silently on the echo.
    records = _opted_in(tmp_path)
    manifest_path = tmp_path / _MANIFEST_REL
    manifest_path.write_bytes(manifest_path.read_bytes() + b"\n")
    assert (
        pd.resolve_template_pack_echo(tmp_path, "packs/toy/hints.json", records_by_pack=records)
        is None
    )


# --- compose_audit_template: the ONE selection definition (run-#13 finding 1) ---
#
# run-#13 finding 1 RETIRED the receipt_bindings tiebreak (it silently picked the
# wrong pack for the two-layer domain/program split, invisible until the sign-off
# surface). Selection is now the no-heuristics law: one candidate wins; among
# many, the unique derivation-edge survivor (the derived / most-specific template)
# wins; any other shape (no lineage, siblings, or a cycle) refuses LOUDLY naming
# every candidate. A manifest that fails to load is NAMED (``skipped``), never
# silently dropped. Cases needing a real ``derived_from`` lineage seal it through
# the WS5 reseal recipe passthrough and activate on the WS5 rebase — the
# field-presence skipif flips them on automatically.

_DERIVED_FROM_LIVE = "derived_from" in {f.name for f in dataclasses.fields(pack.PackManifest)}
_needs_ws5 = pytest.mark.skipif(
    not _DERIVED_FROM_LIVE,
    reason="WS5 seam not yet in tree: PackManifest.derived_from + reseal passthrough",
)


def _derives_from(parent: str, *, seam: str = "audit_template") -> dict[str, str]:
    """A ``derived_from`` recipe value naming *parent*'s *seam* (WS5 passthrough)."""
    return {"pack": parent, "seam": seam, "version": "1.0.0", "sha": "0" * 64}


def _seal_seam_pack(base: Path, name: str, *, derived_from: dict[str, str] | None = None) -> str:
    """Seal a minimal manifest with an ``audit_template`` seam; return manifest rel.

    *derived_from* (WS5 reseal recipe passthrough) stamps the derivation lineage
    that the edge-elimination reads.
    """
    from hpc_agent.state.pack_sweep import reseal_manifest

    root = base / "packs" / name
    (root / "templates").mkdir(parents=True, exist_ok=True)
    (root / "templates" / "audit.py").write_text(f"# %% {name}\n", encoding="utf-8")
    recipe: dict[str, Any] = {
        "name": name,
        "version": "1.0.0",
        "seams": {"audit_template": "templates/audit.py"},
        "fills_slots": [],
        "pack_files": ["templates/audit.py"],
        "sweep": [],
    }
    if derived_from is not None:
        recipe["derived_from"] = derived_from
    (root / "sweep.json").write_text(json.dumps(recipe), encoding="utf-8")
    rel = f"packs/{name}/manifest.json"
    reseal_manifest(base / rel, root / "sweep.json")
    return rel


def _optin(name: str, rel: str) -> dict[str, Any]:
    return {"pack": name, "manifest": rel, "receipt_bindings": []}


# (1) one candidate wins — rule='single_candidate', candidates names it. PASSES NOW.
def test_compose_audit_template_single_candidate_wins(tmp_path: Path) -> None:
    rel = _seal_seam_pack(tmp_path, "alpha")
    chosen = pd.compose_audit_template([_optin("alpha", rel)], tmp_path)
    assert chosen is not None
    assert chosen["pack"] == "alpha"
    assert chosen["value"] == "packs/alpha/templates/audit.py"
    assert chosen["source"] == "pack_audit_template_seam"
    assert chosen["rule"] == "single_candidate"
    assert chosen["candidates"] == "alpha:packs/alpha/templates/audit.py"


# (2) two candidates + a derived_from edge → the DERIVED template wins; the
# disclosure names BOTH candidates + rule='derivation_edge'. ACTIVATES ON REBASE.
@_needs_ws5
def test_compose_audit_template_derived_edge_wins(tmp_path: Path) -> None:
    skel = _seal_seam_pack(tmp_path, "skel")
    prog = _seal_seam_pack(tmp_path, "prog", derived_from=_derives_from("skel"))
    chosen = pd.compose_audit_template([_optin("skel", skel), _optin("prog", prog)], tmp_path)
    assert chosen is not None
    assert chosen["pack"] == "prog"
    assert chosen["value"] == "packs/prog/templates/audit.py"
    assert chosen["rule"] == "derivation_edge"
    assert "skel:" in chosen["candidates"] and "prog:" in chosen["candidates"]


# (3) chain A<-B<-C → the deepest derivative wins. ACTIVATES ON REBASE.
@_needs_ws5
def test_compose_audit_template_chain_deepest_wins(tmp_path: Path) -> None:
    a = _seal_seam_pack(tmp_path, "aaa")
    b = _seal_seam_pack(tmp_path, "bbb", derived_from=_derives_from("aaa"))
    c = _seal_seam_pack(tmp_path, "ccc", derived_from=_derives_from("bbb"))
    chosen = pd.compose_audit_template(
        [_optin("aaa", a), _optin("bbb", b), _optin("ccc", c)], tmp_path
    )
    assert chosen is not None and chosen["pack"] == "ccc"
    assert chosen["rule"] == "derivation_edge"


# (4) sibling derivatives of one skeleton → refuse naming ALL THREE. REBASE.
@_needs_ws5
def test_compose_audit_template_sibling_derivatives_refuse(tmp_path: Path) -> None:
    skel = _seal_seam_pack(tmp_path, "skel")
    p1 = _seal_seam_pack(tmp_path, "prog-one", derived_from=_derives_from("skel"))
    p2 = _seal_seam_pack(tmp_path, "prog-two", derived_from=_derives_from("skel"))
    with pytest.raises(errors.SpecInvalid) as exc:
        pd.compose_audit_template(
            [_optin("skel", skel), _optin("prog-one", p1), _optin("prog-two", p2)], tmp_path
        )
    msg = str(exc.value)
    assert "skel" in msg and "prog-one" in msg and "prog-two" in msg


# (5) two candidates, NO edge → SpecInvalid naming both + the remedy. This is the
# enforcement-map FIRE test (DC4). ACTIVATES ON REBASE (reads derived_from).
@_needs_ws5
def test_compose_audit_template_multi_candidate_no_edge_refuses(tmp_path: Path) -> None:
    rel_a = _seal_seam_pack(tmp_path, "alpha")
    rel_b = _seal_seam_pack(tmp_path, "beta")
    with pytest.raises(errors.SpecInvalid) as exc:
        pd.compose_audit_template([_optin("alpha", rel_a), _optin("beta", rel_b)], tmp_path)
    msg = str(exc.value)
    assert "alpha" in msg and "beta" in msg
    # The remedy must read for record_interview (the universal submit intake).
    assert "audited_source.template" in msg
    assert "derived_from" in msg


# (6) derived_from pointing at a NON-candidate pack, or the WRONG seam, is no
# edge → refuse. ACTIVATES ON REBASE.
@_needs_ws5
def test_compose_audit_template_derived_from_non_candidate_is_no_edge(tmp_path: Path) -> None:
    rel_a = _seal_seam_pack(tmp_path, "alpha")
    rel_b = _seal_seam_pack(tmp_path, "beta", derived_from=_derives_from("ghost-parent"))
    with pytest.raises(errors.SpecInvalid):
        pd.compose_audit_template([_optin("alpha", rel_a), _optin("beta", rel_b)], tmp_path)


@_needs_ws5
def test_compose_audit_template_derived_from_wrong_seam_is_no_edge(tmp_path: Path) -> None:
    rel_a = _seal_seam_pack(tmp_path, "alpha")
    rel_b = _seal_seam_pack(
        tmp_path, "beta", derived_from=_derives_from("alpha", seam="reader_calls")
    )
    with pytest.raises(errors.SpecInvalid):
        pd.compose_audit_template([_optin("alpha", rel_a), _optin("beta", rel_b)], tmp_path)


# (7) zero-survivor: a MUTUAL derivation cycle eliminates every candidate → refuse
# (distinct from the None-when-no-candidates contract). ACTIVATES ON REBASE.
@_needs_ws5
def test_compose_audit_template_mutual_cycle_refuses(tmp_path: Path) -> None:
    a = _seal_seam_pack(tmp_path, "aaa", derived_from=_derives_from("bbb"))
    b = _seal_seam_pack(tmp_path, "bbb", derived_from=_derives_from("aaa"))
    with pytest.raises(errors.SpecInvalid) as exc:
        pd.compose_audit_template([_optin("aaa", a), _optin("bbb", b)], tmp_path)
    assert "cycle" in str(exc.value)
    assert "aaa" in str(exc.value) and "bbb" in str(exc.value)


# A lone self-deriving pack has nothing to disambiguate → single_candidate (None
# is returned ONLY when zero candidates existed before elimination). REBASE (seal).
@_needs_ws5
def test_compose_audit_template_self_edge_single_candidate_still_wins(tmp_path: Path) -> None:
    rel = _seal_seam_pack(tmp_path, "alpha", derived_from=_derives_from("alpha"))
    chosen = pd.compose_audit_template([_optin("alpha", rel)], tmp_path)
    assert chosen is not None and chosen["rule"] == "single_candidate"


# A candidate whose manifest fails to load is NAMED in ``skipped``, never dropped;
# here one good survivor remains → single_candidate + skipped names the broken one.
# PASSES NOW (single survivor short-circuits before any derived_from read).
def test_compose_audit_template_skip_disclosed_names_broken_manifest(tmp_path: Path) -> None:
    good = _seal_seam_pack(tmp_path, "alpha")
    packs = [_optin("alpha", good), _optin("broken", "packs/broken/manifest.json")]
    chosen = pd.compose_audit_template(packs, tmp_path)
    assert chosen is not None
    assert chosen["pack"] == "alpha"
    assert chosen["rule"] == "single_candidate"
    assert "skipped" in chosen
    assert "broken" in chosen["skipped"]


# A MALFORMED derived_from is a loud WS5 parse SpecInvalid → the candidate is
# skipped-AND-named (a lineage typo surfaces, never a silent confident wrong pick).
# ACTIVATES ON REBASE (needs WS5's malformed-derived_from parse refusal).
@_needs_ws5
def test_compose_audit_template_malformed_derived_from_surfaces(tmp_path: Path) -> None:
    good = _seal_seam_pack(tmp_path, "alpha")
    beta_root = tmp_path / "packs" / "beta"
    (beta_root / "templates").mkdir(parents=True)
    body = b"# %% beta\n"
    (beta_root / "templates" / "audit.py").write_bytes(body)
    manifest = {
        "name": "beta",
        "version": "1.0.0",
        "files": [{"path": "templates/audit.py", "sha256": _raw_sha(body)}],
        "seams": {"audit_template": "templates/audit.py"},
        "fills_slots": [],
        "derived_from": {"pack": "alpha"},  # malformed: missing seam/version/sha
    }
    (beta_root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    packs = [_optin("alpha", good), _optin("beta", "packs/beta/manifest.json")]
    chosen = pd.compose_audit_template(packs, tmp_path)
    assert chosen is not None and chosen["pack"] == "alpha"
    assert "skipped" in chosen and "beta" in chosen["skipped"]


def test_compose_audit_template_none_when_nothing_composes(tmp_path: Path) -> None:
    # Empty opt-in / unreadable manifests → None (the audit-preflight seat then
    # refuses LOUDLY; pinned in tests/ops/test_audit_preflight.py).
    assert pd.compose_audit_template([], tmp_path) is None
    packs = [{"pack": "ghost", "manifest": "packs/ghost/manifest.json"}]
    assert pd.compose_audit_template(packs, tmp_path) is None


def test_compose_audit_template_source_never_reads_receipt_bindings() -> None:
    # The retired heuristic must be UNRETURNABLE: NO code in the selection
    # definition reads 'receipt_bindings' (the docstring may name the retired
    # tiebreak in prose, so the pin is AST-based and excludes the docstring —
    # the enforcement-map fire path against a silently-regrown tiebreak).
    import inspect

    fn = ast.parse(inspect.getsource(pd.compose_audit_template)).body[0]
    assert isinstance(fn, ast.FunctionDef)
    body = fn.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]  # drop the docstring node
    dumped = "\n".join(ast.dump(node) for node in body)
    assert "receipt_bindings" not in dumped


def test_compose_audit_template_disclosure_values_are_all_str(tmp_path: Path) -> None:
    # Wire-shape pin: composed_defaults persists as dict[str, str]; every value
    # (incl. the ADDED rule/candidates keys) is a str, so an interview.json
    # round-trip is byte-safe. Pack slugs cannot contain ':' (validate_tag), so
    # the 'pack:relpath' candidates join parses unambiguously.
    rel = _seal_seam_pack(tmp_path, "alpha")
    chosen = pd.compose_audit_template([_optin("alpha", rel)], tmp_path)
    assert chosen is not None
    assert all(isinstance(v, str) for v in chosen.values())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
