"""Tests for ``state/data_trace.py`` (T1) — toy text/CSV-shaped dicts only.

No quant vocabulary: stages are ``load``/``dedup``/``join``, columns are
``id``/``name``/``qty``, labels are ``coord_space`` with values ``raw``/``norm``.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent.state import data_trace as dt

# --- helpers -----------------------------------------------------------------


def _rec(stage, seq, atoms, **kw):
    return dt.make_record(stage, seq, atoms, **kw)


def _row(rows, dropped=0):
    return {"row_count": {"rows": rows, "dropped": dropped}}


# --- the registry: equality pin + per-atom semantics dispatch ----------------


def test_registry_is_the_equality_pinned_closed_set():
    # One registry, one closed set — render/diff/fingerprint all key on this.
    assert (
        frozenset(
            {
                "row_count",
                "col_set",
                "null_count",
                "value_sketch",
                "span",
                "order_integrity",
                "label_chain",
                "digest",
                "duration_ms",
                "peak_mb",
            }
        )
        == dt.ATOM_NAMES
    )
    assert set(dt.ATOM_REGISTRY) == set(dt.ATOM_NAMES)


def test_comparison_semantics_dispatch_per_atom():
    expected = {
        "row_count": "exact",
        "col_set": "set-delta",
        "null_count": "exact-per-key",
        "value_sketch": "tolerance",
        "span": "exact-endpoints",
        "order_integrity": "exact",
        "label_chain": "equality-chain",
        "digest": "exact",
        "duration_ms": "tolerance",
        "peak_mb": "tolerance",
    }
    for name, comparison in expected.items():
        assert dt.comparison_for(name) == comparison
        assert dt.atom_schema(name).comparison == comparison


def test_openlineage_facet_notes_present_where_a13_maps_them():
    # A13 courtesy mapping: these atoms carry a facet note; the core-original
    # atoms carry None.
    assert dt.atom_schema("row_count").openlineage_facet is not None
    assert dt.atom_schema("null_count").openlineage_facet is not None
    assert dt.atom_schema("value_sketch").openlineage_facet is not None
    assert dt.atom_schema("label_chain").openlineage_facet is None
    assert dt.atom_schema("span").openlineage_facet is None


def test_atom_schema_unknown_raises():
    with pytest.raises(errors.SpecInvalid):
        dt.atom_schema("not_an_atom")


# --- the record model + validation -------------------------------------------


def test_make_record_happy_and_defaults():
    rec = _rec("load", 0, _row(10), section="ingest")
    assert rec["trace_schema_version"] == dt.TRACE_SCHEMA_VERSION
    assert rec["section"] == "ingest"
    assert rec["created_at"]  # auto-stamped
    assert dt.validate_record(rec) == []


def test_make_record_rejects_unknown_atom():
    with pytest.raises(errors.SpecInvalid):
        _rec("load", 0, {"bogus_atom": 1})


def test_validate_record_catches_shape_violations():
    # bad seq, bad row_count value, unknown atom, malformed flag
    bad = {
        "stage": "load",
        "seq": -1,
        "atoms": {"row_count": {"rows": "ten", "dropped": 0}, "mystery": 1},
        "flags": [{"detail": "no rule"}],
        "trace_schema_version": 1,
        "created_at": "2026-07-08T00:00:00Z",
    }
    errs = dt.validate_record(bad)
    assert any("seq" in e for e in errs)
    assert any("rows" in e for e in errs)
    assert any("mystery" in e for e in errs)
    assert any("flag.rule" in e for e in errs)


def test_value_sketch_quantiles_are_fixed():
    good = {
        "value_sketch": {
            "qty": {
                "min": 0,
                "max": 9,
                "mean": 4.5,
                "std": 2.0,
                "quantiles": {"q05": 0.5, "q50": 4.0, "q95": 8.5},
            }
        }
    }
    assert dt.validate_record(_rec("x", 0, good)) == []
    # an extra quantile key is rejected (FIXED q05/q50/q95)
    bad = json.loads(json.dumps(good))
    bad["value_sketch"]["qty"]["quantiles"]["q99"] = 9.0
    errs = dt.validate_record(
        {
            "stage": "x",
            "seq": 0,
            "atoms": bad,
            "flags": [],
            "trace_schema_version": 1,
            "created_at": "t",
        }
    )
    assert any("FIXED" in e for e in errs)


def test_make_flag_shape():
    flag = dt.make_flag("row_conservation", "rows off", {"stage": "join"})
    assert flag == {"rule": "row_conservation", "detail": "rows off", "evidence": {"stage": "join"}}
    with pytest.raises(errors.SpecInvalid):
        dt.make_flag("", "detail")


# --- canonical serialization -------------------------------------------------


def test_records_sha_is_canonical_and_order_sensitive():
    a = _rec("load", 0, _row(10))
    b = _rec("dedup", 1, _row(8, dropped=2))
    sha1 = dt.records_sha([a, b])
    sha2 = dt.records_sha([a, b])
    assert sha1 == sha2 == dt.records_sha([a, b])
    assert dt.records_sha([b, a]) != sha1  # order matters
    # routes through the one canonical helper
    from hpc_agent.state.determinism import canonical_sha

    assert dt.records_sha([a, b]) == canonical_sha([a, b])


# --- invariants: each fires on a synthetic violation + passes happy ----------


def test_row_conservation_passes_and_fires():
    happy = [_rec("load", 0, _row(10)), _rec("dedup", 1, _row(8, dropped=2))]
    assert dt.check_row_conservation(happy) == []

    # 10 in, drop 2, but 7 out (should be 8) → one flag
    broken = [_rec("load", 0, _row(10)), _rec("dedup", 1, _row(7, dropped=2))]
    flags = dt.check_row_conservation(broken)
    assert len(flags) == 1
    assert flags[0]["rule"] == "row_conservation"
    assert flags[0]["evidence"]["expected"] == 8


def test_label_chain_continuity_passes_and_fires():
    lbl = lambda v: {"label_chain": {"coord_space": v}}  # noqa: E731
    happy = [
        _rec("load", 0, {**_row(10), **lbl("raw")}),
        _rec("norm", 1, {**_row(10), **lbl("norm")}),
    ]
    assert dt.check_label_chain_continuity(happy) == []

    # label introduced at stage 0, then VANISHES at stage 1 → break
    broken = [
        _rec("load", 0, {**_row(10), **lbl("raw")}),
        _rec("drop_it", 1, _row(10)),
    ]
    flags = dt.check_label_chain_continuity(broken)
    assert len(flags) == 1
    assert flags[0]["rule"] == "label_chain_break"
    assert flags[0]["evidence"]["label"] == "coord_space"


def test_seq_monotonicity_passes_and_fires():
    happy = [_rec("a", 0, _row(1)), _rec("b", 1, _row(1)), _rec("c", 2, _row(1))]
    assert dt.check_seq_monotonicity(happy) == []

    # duplicate seq → not strictly increasing
    broken = [_rec("a", 0, _row(1)), _rec("b", 1, _row(1)), _rec("c", 1, _row(1))]
    flags = dt.check_seq_monotonicity(broken)
    assert len(flags) == 1
    assert flags[0]["rule"] == "seq_monotonicity"


def test_run_invariants_aggregates():
    broken = [_rec("load", 0, _row(10)), _rec("dedup", 1, _row(7, dropped=2))]
    rules = {f["rule"] for f in dt.run_invariants(broken)}
    assert "row_conservation" in rules


# --- the store: round-trip + tolerant read -----------------------------------


def test_store_round_trip(tmp_path: Path):
    recs = [_rec("load", 0, _row(10)), _rec("dedup", 1, _row(8, dropped=2))]
    path = dt.write_trace(tmp_path, "run", "run-abc", 0, recs)
    assert path.name == "task-0.jsonl"
    assert "traces" in path.parts and "run" in path.parts and "run-abc" in path.parts
    got = dt.read_trace(tmp_path, "run", "run-abc", 0)
    assert got == recs


def test_read_trace_tolerant_of_corruption_and_missing(tmp_path: Path):
    # missing file → []
    assert dt.read_trace(tmp_path, "run", "nope", 0) == []
    # corrupt line is skipped, good lines survive
    recs = [_rec("load", 0, _row(10))]
    path = dt.write_trace(tmp_path, "audit", "audit-1", 0, recs)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{ not json\n\n")
    got = dt.read_trace(tmp_path, "audit", "audit-1", 0)
    assert got == recs


def test_write_trace_rejects_invalid_record(tmp_path: Path):
    with pytest.raises(errors.SpecInvalid):
        dt.write_trace(tmp_path, "run", "r", 0, [{"stage": "x", "seq": 0}])


def test_store_scope_validation(tmp_path: Path):
    with pytest.raises(errors.SpecInvalid):
        dt.trace_store_path(tmp_path, "bogus", "id", 0)
    with pytest.raises(errors.SpecInvalid):
        dt.trace_store_path(tmp_path, "run", "../escape", 0)
    with pytest.raises(errors.SpecInvalid):
        dt.trace_store_path(tmp_path, "run", "id", -1)


# --- ingest: moves + journals + trace_sha recomputes -------------------------


def _write_transport(path: Path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def test_ingest_moves_journals_and_sha_recomputes(tmp_path: Path):
    from hpc_agent.state.decision_journal import read_decisions

    recs = [_rec("load", 0, _row(10)), _rec("dedup", 1, _row(8, dropped=2))]
    transport = tmp_path / "out" / "_trace.jsonl"
    _write_transport(transport, recs)

    summary = dt.ingest_trace(tmp_path, "run", "run-xyz", 0, transport)

    # moved into the store; transport gone
    assert not transport.exists()
    assert dt.read_trace(tmp_path, "run", "run-xyz", 0) == recs

    # trace_sha recomputes over the stored records
    assert summary["trace_sha"] == dt.records_sha(recs)
    assert summary["stage_count"] == 2

    # exactly ONE journal record, on the mapped scope, block="data-trace"
    decisions = read_decisions(tmp_path, "run", "run-xyz")
    dtr = [d for d in decisions if d["block"] == dt.DATA_TRACE_BLOCK]
    assert len(dtr) == 1
    resolved = dtr[0]["resolved"]
    assert resolved["scope"] == "run"
    assert resolved["id"] == "run-xyz"
    assert resolved["task"] == 0
    assert resolved["trace_sha"] == summary["trace_sha"]
    assert resolved["stage_count"] == 2


def test_ingest_audit_scope_journals_under_notebook(tmp_path: Path):
    from hpc_agent.state.decision_journal import read_decisions

    recs = [_rec("load", 0, _row(3))]
    transport = tmp_path / "_trace.jsonl"
    _write_transport(transport, recs)
    summary = dt.ingest_trace(tmp_path, "audit", "audit-77", 0, transport)
    # audit scope → journal under notebook scope, id == audit_id
    assert summary["journal_scope_kind"] == "notebook"
    assert summary["journal_scope_id"] == "audit-77"
    assert read_decisions(tmp_path, "notebook", "audit-77")


def test_ingest_local_scope_journals_under_scope(tmp_path: Path):
    recs = [_rec("load", 0, _row(3))]
    transport = tmp_path / "_trace.jsonl"
    _write_transport(transport, recs)
    summary = dt.ingest_trace(tmp_path, "local", "deadbeef1234", 0, transport)
    assert summary["journal_scope_kind"] == "scope"
    assert summary["journal_scope_id"] == "deadbeef1234"


def test_ingest_rejects_invalid_line(tmp_path: Path):
    transport = tmp_path / "_trace.jsonl"
    transport.write_text('{"stage": "x", "seq": 0}\n', encoding="utf-8")
    with pytest.raises(errors.SpecInvalid):
        dt.ingest_trace(tmp_path, "run", "r", 0, transport)


# --- the block-class pin: data-trace never enters an attestation reduction ----


def test_data_trace_block_absent_from_notebook_attestation():
    from hpc_agent.state import notebook_audit as na

    assert dt.DATA_TRACE_BLOCK not in na._BLOCK_ATTESTOR
    # a data-trace journal record projects to nothing in both reductions
    fake = {"block": dt.DATA_TRACE_BLOCK, "resolved": {"trace_sha": "abc", "stage_count": 1}}
    assert na._project(fake) is None
    assert na._project_receipt(fake) is None


# --- the stdlib-only import pin (AST) ----------------------------------------


def test_module_is_stdlib_only_no_pandas_numpy():
    src = Path(dt.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    forbidden = {"pandas", "numpy", "pd", "np", "pyarrow", "polars"}
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0])
    assert not (imported_roots & forbidden), f"forbidden imports: {imported_roots & forbidden}"
