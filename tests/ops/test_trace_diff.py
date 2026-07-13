"""Tests for ``ops/trace_diff_op.py`` (T6) — toy text/CSV-shaped dicts only.

No quant vocabulary: stages are ``load``/``dedup``/``join``/``score``, columns
are ``id``/``name``/``qty``, labels are ``coord_space`` with values
``raw``/``norm``. The acceptance case (design §"Acceptance"): two synthetic
traces diverging at a KNOWN stage localize EXACTLY there via first-divergence.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import hpc_agent.ops.trace_diff_op as td
import hpc_agent.state.data_trace as dt

# --- helpers -----------------------------------------------------------------


def _rec(stage, seq, atoms, **kw):
    return dt.make_record(stage, seq, atoms, **kw)


def _row(rows, dropped=0):
    return {"row_count": {"rows": rows, "dropped": dropped}}


def _sketch(col, mean, std=1.0, mn=0.0, mx=10.0, q=(1.0, 5.0, 9.0)):
    return {
        "value_sketch": {
            col: {
                "min": mn,
                "max": mx,
                "mean": mean,
                "std": std,
                "quantiles": {"q05": q[0], "q50": q[1], "q95": q[2]},
            }
        }
    }


def _write(tmp: Path, scope_id, records, *, scope_kind="run", task=0):
    dt.write_trace(tmp, scope_kind, scope_id, task, records)


def _diff(tmp: Path, a_id, b_id, tolerance=None, **key_kw):
    spec = td.TraceDiffSpec.model_validate(
        {
            "a": {"scope_kind": "run", "scope_id": a_id, "task": 0},
            "b": {"scope_kind": "run", "scope_id": b_id, "task": 0},
            **({"tolerance": tolerance} if tolerance is not None else {}),
        }
    )
    return td.trace_diff(tmp, spec=spec)


# --- the acceptance case: first-divergence localizes EXACTLY -----------------


def test_planted_divergence_localizes_at_the_known_stage(tmp_path):
    # A four-stage pipeline; B drops one extra row at `dedup` (seq 2). The
    # divergence must localize to `dedup`, not `load` (before) or `join` (after).
    common = [
        _rec("load", 1, _row(100)),
        _rec("join", 3, {"col_set": {"columns": ["id", "name", "qty"]}}),
    ]
    a = [_rec("load", 1, _row(100)), _rec("dedup", 2, _row(90, dropped=10)), common[1]]
    b = [_rec("load", 1, _row(100)), _rec("dedup", 2, _row(85, dropped=15)), common[1]]
    _write(tmp_path, "run-a", a)
    _write(tmp_path, "run-b", b)

    res = _diff(tmp_path, "run-a", "run-b")

    assert res.clean is False
    assert res.first_divergence is not None
    assert res.first_divergence.stage == "dedup"
    assert res.first_divergence.seq == 2
    assert res.first_divergence.atom == "row_count"
    assert res.first_divergence.kind == "exact"
    # load (seq 1) is identical, so it is NOT the first divergence.
    load_stage = next(s for s in res.stages if s["stage"] == "load")
    assert load_stage["divergences"] == []


def test_identical_traces_are_clean(tmp_path):
    recs = [_rec("load", 1, _row(100)), _rec("dedup", 2, _row(90, dropped=10))]
    _write(tmp_path, "run-a", [_rec("load", 1, _row(100)), _rec("dedup", 2, _row(90, dropped=10))])
    _write(tmp_path, "run-b", recs)
    res = _diff(tmp_path, "run-a", "run-b")
    assert res.clean is True
    assert res.aligned is True
    assert res.first_divergence is None
    assert res.structural == []
    assert "identical" in res.render


# --- each semantics kind fires -----------------------------------------------


def test_exact_row_count_mismatch(tmp_path):
    _write(tmp_path, "a", [_rec("load", 1, _row(100))])
    _write(tmp_path, "b", [_rec("load", 1, _row(90))])
    res = _diff(tmp_path, "a", "b")
    detail = res.stages[0]["divergences"][0]["detail"]
    assert detail == "row_count rows 100 → 90"


def test_set_delta_col_set(tmp_path):
    _write(tmp_path, "a", [_rec("join", 1, {"col_set": {"columns": ["id", "name"]}})])
    _write(tmp_path, "b", [_rec("join", 1, {"col_set": {"columns": ["id", "qty"]}})])
    res = _diff(tmp_path, "a", "b")
    d = res.stages[0]["divergences"][0]
    assert d["kind"] == "set-delta"
    assert d["detail"] == "col_set added ['qty'] dropped ['name']"


def test_exact_per_key_null_count(tmp_path):
    _write(tmp_path, "a", [_rec("load", 1, {"null_count": {"id": 0, "name": 2}})])
    _write(tmp_path, "b", [_rec("load", 1, {"null_count": {"id": 0, "name": 5}})])
    res = _diff(tmp_path, "a", "b")
    d = res.stages[0]["divergences"][0]
    assert d["kind"] == "exact-per-key"
    assert d["detail"] == "null_count[name] 2 → 5"


def test_tolerance_inside_and_outside(tmp_path):
    _write(tmp_path, "a", [_rec("fit", 1, _sketch("qty", mean=5.0))])
    _write(tmp_path, "b", [_rec("fit", 1, _sketch("qty", mean=5.05))])

    # No tolerance → EXACT → the 0.05 mean shift is a divergence.
    res_exact = _diff(tmp_path, "a", "b")
    assert res_exact.clean is False
    assert any("mean" in d["detail"] for d in res_exact.stages[0]["divergences"])

    # Caller tolerance abs_tol=0.1 → INSIDE → clean.
    res_in = _diff(tmp_path, "a", "b", tolerance={"default_abs_tol": 0.1})
    assert res_in.clean is True

    # Caller tolerance abs_tol=0.01 → OUTSIDE → divergence.
    res_out = _diff(tmp_path, "a", "b", tolerance={"default_abs_tol": 0.01})
    assert res_out.clean is False


def test_equality_chain_label_break(tmp_path):
    _write(tmp_path, "a", [_rec("scale", 1, {"label_chain": {"coord_space": "norm"}})])
    _write(tmp_path, "b", [_rec("scale", 1, {"label_chain": {"coord_space": "raw"}})])
    res = _diff(tmp_path, "a", "b")
    d = res.stages[0]["divergences"][0]
    assert d["kind"] == "equality-chain"
    assert d["detail"] == "label_chain[coord_space] 'norm' → 'raw'"


def test_exact_endpoints_span(tmp_path):
    _write(tmp_path, "a", [_rec("load", 1, {"span": {"id": {"first": 1, "last": 100}}})])
    _write(tmp_path, "b", [_rec("load", 1, {"span": {"id": {"first": 1, "last": 90}}})])
    res = _diff(tmp_path, "a", "b")
    d = res.stages[0]["divergences"][0]
    assert d["kind"] == "exact-endpoints"
    assert d["detail"] == "span[id].last 100 → 90"


# --- structural divergence (a missing stage) ---------------------------------


def test_structural_divergence_missing_stage(tmp_path):
    # B has an extra `score` stage (seq 3) A lacks — a named structural divergence.
    a = [_rec("load", 1, _row(100)), _rec("dedup", 2, _row(90, dropped=10))]
    b = [
        _rec("load", 1, _row(100)),
        _rec("dedup", 2, _row(90, dropped=10)),
        _rec("score", 3, _row(90)),
    ]
    _write(tmp_path, "run-a", a)
    _write(tmp_path, "run-b", b)
    res = _diff(tmp_path, "run-a", "run-b")

    assert res.aligned is False
    assert res.clean is False
    assert len(res.structural) == 1
    flag = res.structural[0]
    assert flag["rule"] == "stage_unmatched"
    assert flag["evidence"]["side"] == "b_only"
    # The structural stage is at seq 3, AFTER the identical stages → first
    # divergence is the structural one (nothing parted earlier).
    assert res.first_divergence is not None
    assert res.first_divergence.stage == "score"
    assert res.first_divergence.kind == "structural"


def test_absence_is_disclosed(tmp_path):
    # A exists, B was never written → B disclosed absent, never fabricated match.
    _write(tmp_path, "run-a", [_rec("load", 1, _row(100))])
    res = _diff(tmp_path, "run-a", "run-b-missing")
    assert res.a.present is True
    assert res.b.present is False
    assert res.b.stage_count == 0
    assert res.clean is False
    assert "absent (no trace in the store)" in res.render


def test_both_absent_is_a_clean_empty_diff(tmp_path):
    res = _diff(tmp_path, "nope-a", "nope-b")
    assert res.a.present is False and res.b.present is False
    assert res.clean is True
    assert res.first_divergence is None


# --- the pins ----------------------------------------------------------------


def test_route_through_comparison_for_pin():
    """The diff dispatches ONLY through T1's ``comparison_for`` — no local table.

    Route-through pin: the module obtains an atom's semantics from
    ``comparison_for`` (the ONE registry), never a hand-rolled ``{atom:
    semantics}`` map that could drift from T1.
    """
    src = inspect.getsource(td)
    assert "comparison_for(" in src
    # No local atom→semantics table: none of the atom names is mapped to a
    # semantics token by a literal ``"<atom>": "<semantics>"`` pair.
    semantics_tokens = {
        "exact",
        "set-delta",
        "tolerance",
        "exact-per-key",
        "equality-chain",
        "exact-endpoints",
    }
    for atom in dt.ATOM_NAMES:
        for tok in semantics_tokens:
            assert f'"{atom}": "{tok}"' not in src
            assert f"'{atom}': '{tok}'" not in src


def test_render_carries_no_verdict_vocabulary(tmp_path):
    """The token pin: the render states FACTS, never a verdict."""
    _write(tmp_path, "a", [_rec("load", 1, _row(100))])
    _write(tmp_path, "b", [_rec("load", 1, _row(90))])
    render = _diff(tmp_path, "a", "b").render.lower()
    forbidden = (
        "wrong",
        "incorrect",
        "correct",
        "invalid",
        " valid",
        "broken",
        "error",
        "fail",
        "pass",
        "bad ",
        "good ",
        "success",
        "mismatch",
    )
    for word in forbidden:
        assert word not in render, f"verdict vocabulary {word!r} leaked into the render"
    # It DOES carry the fact.
    assert "row_count rows 100 → 90" in render


def test_render_is_byte_stable(tmp_path):
    """Deterministic render — same inputs, byte-identical output across calls."""
    a = [_rec("load", 1, _row(100)), _rec("dedup", 2, _row(90, dropped=10))]
    b = [_rec("load", 1, _row(100)), _rec("dedup", 2, _row(80, dropped=20))]
    _write(tmp_path, "run-a", a)
    _write(tmp_path, "run-b", b)
    r1 = _diff(tmp_path, "run-a", "run-b").render
    r2 = _diff(tmp_path, "run-a", "run-b").render
    assert r1 == r2


def test_tolerance_absent_means_exact(tmp_path):
    """No tolerance → duration_ms compared EXACTLY (no invented epsilon)."""
    _write(tmp_path, "a", [_rec("fit", 1, {"duration_ms": 100.0})])
    _write(tmp_path, "b", [_rec("fit", 1, {"duration_ms": 100.1})])
    res = _diff(tmp_path, "a", "b")
    assert res.clean is False
    assert res.stages[0]["divergences"][0]["detail"] == "duration_ms 100.0 → 100.1"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
