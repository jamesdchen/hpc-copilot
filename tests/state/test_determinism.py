"""Tests for the determinism-fingerprint kernel (``state/determinism.py``, T1).

Toy WIDGET vocabulary only — never quant/harxhar words (the toy-fixture rule).
Covers: envelope honesty (range-only at n=2), no-invented-tolerance, thin-vs-
evidenced routing BOTH directions, identity-drift + data-identity staleness,
the admission gate (unadmitted never moves an envelope nor satisfies a demand),
the static classifier, shape/key-set exact-class violations, caller-override
labeling, ``evidence_meets`` shortfalls + unknown-key refusal, and the AST /
import pins (no numeric tolerance literal in the classifier, no ``statistics``).
"""

from __future__ import annotations

import ast
import inspect
import math

import pytest

from hpc_agent import errors
from hpc_agent.state import determinism as d

_CMD = "c" * 64
_TASKS = "t" * 64
_EXEC = "widget_bench.py"
_IDENT = {"cmd_sha": _CMD, "tasks_py_sha": _TASKS, "executor": _EXEC}
_TS = "2026-07-08T00:00:00+00:00"


def _pk(key: str, a: object, b: object) -> d.PerKeyDiff:
    """One PerKeyDiff via the real diff path (so comparability matches prod)."""
    return d.diff_metrics({key: a}, {key: b})[0]


def _sample(
    per_key: list[d.PerKeyDiff],
    *,
    identity: dict[str, object] | None = None,
    source: str = "double-canary",
    run_ids: list[str] | None = None,
    cluster: str = "hoffman2",
    scale: str = "canary",
    verdict: str = "auto_cleared",
    same_submission: bool = True,
    partial: bool = False,
    task_indices: list[int] | None = None,
) -> d.Sample:
    rec = d.build_sample_record(
        ts=_TS,
        content_sha=d.compute_content_sha({"a": 1}, {"b": 2}),
        identity=identity or dict(_IDENT),
        source=source,
        run_ids=run_ids or ["widget-run-a", "widget-run-b"],
        cluster=cluster,
        scale=scale,
        verdict=verdict,
        per_key=per_key,
        same_submission=same_submission,
        partial=partial,
        task_indices=task_indices,
    )
    return d.validate_sample(rec)


# --- canonical sha ----------------------------------------------------------


def test_canonical_sha_is_harness_contract_form() -> None:
    obj = {"b": 1, "a": 2.5, "z": "x"}
    import hashlib
    import json

    expected = hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    assert d.canonical_sha(obj) == expected
    # Ordered pair, both payloads folded in.
    assert d.compute_content_sha({"m": 1}, {"m": 2}) != d.compute_content_sha({"m": 2}, {"m": 1})


# --- the static classifier --------------------------------------------------


def test_static_class_per_type() -> None:
    assert d.static_class(1.0) == "float"
    assert d.static_class(3) == "int"
    assert d.static_class("widgets") == "str"
    assert d.static_class(True) == "bool"  # bool BEFORE int
    assert d.static_class(False) == "bool"
    assert d.static_class([1, 2]) == "shape"
    assert d.static_class({"a": 1}) == "shape"
    assert d.static_class(None) == "shape"


def test_flatten_metrics_matches_ops_convention() -> None:
    flat = d.flatten_metrics({"grid0": {"throughput": 1.0}, "flat": 2})
    assert flat == {"grid0.throughput": 1.0, "flat": 2}


# --- sample validation ------------------------------------------------------


def test_build_and_validate_roundtrip() -> None:
    s = _sample([_pk("widgets.rate", 1.0, 1.0)])
    assert s.subject_id == _CMD
    assert s.scale == "canary"
    assert s.per_key[0].static_class == "float"


def test_validate_rejects_wrong_attestor_and_kind() -> None:
    rec = d.build_sample_record(
        ts=_TS,
        content_sha="deadbeef",
        identity=dict(_IDENT),
        source="double-canary",
        run_ids=["a", "b"],
        cluster="hoffman2",
        scale="canary",
        verdict="auto_cleared",
        per_key=[_pk("k", 1.0, 1.0)],
    )
    bad = dict(rec)
    bad["attestor"] = "human"
    with pytest.raises(errors.SpecInvalid):
        d.validate_sample(bad)


def test_partial_without_task_indices_refused() -> None:
    # No-silent-caps: a partial sample MUST name the task indices it compared.
    with pytest.raises(errors.SpecInvalid):
        d.build_sample_record(
            ts=_TS,
            content_sha="x",
            identity=dict(_IDENT),
            source="verify-reproduction",
            run_ids=["a", "b"],
            cluster="hoffman2",
            scale="main",
            verdict="auto_cleared",
            per_key=[_pk("k", 1.0, 1.0)],
            partial=True,
            task_indices=None,
        )
    # With indices it is accepted.
    ok = d.build_sample_record(
        ts=_TS,
        content_sha="x",
        identity=dict(_IDENT),
        source="verify-reproduction",
        run_ids=["a", "b"],
        cluster="hoffman2",
        scale="main",
        verdict="auto_cleared",
        per_key=[_pk("k", 1.0, 1.0)],
        partial=True,
        task_indices=[0, 4, 8],
    )
    assert d.validate_sample(ok).task_indices == (0, 4, 8)


# --- envelope honesty (order statistics only, n=2 = the two points) ---------


def test_envelope_honesty_n2_equals_two_points() -> None:
    s1 = _sample([_pk("widgets.rate", 1.0, 1.0)])
    s2 = _sample([_pk("widgets.rate", 1.0, 1.2)])
    env = d.reduce_envelope([s1, s2], [True, True], identity=_IDENT)
    ke = env.per_key["widgets.rate"]
    assert ke.cls == d.STOCHASTIC
    assert ke.lo == 1.0
    assert ke.hi == 1.2  # exactly the observed extremes — no widening
    assert ke.evidence.n == 2
    assert ke.evidence.same_submission_only is True  # both double-canary priors


def test_byte_identical_key_is_exact_class() -> None:
    s1 = _sample([_pk("widgets.rate", 2.0, 2.0)])
    s2 = _sample([_pk("widgets.rate", 2.0, 2.0)])
    env = d.reduce_envelope([s1, s2], [True, True], identity=_IDENT)
    assert env.per_key["widgets.rate"].cls == d.EXACT


# --- no-invented-tolerance --------------------------------------------------


def test_no_invented_tolerance_empty_ledger() -> None:
    env = d.reduce_envelope([], [], identity=_IDENT)
    ulp = math.nextafter(1.0, math.inf)
    diffs = d.diff_metrics({"widgets.rate": 1.0}, {"widgets.rate": ulp})
    result = d.classify(diffs, env, current_scale="main", current_cluster="hoffman2")
    # Two floats differ, empty ledger, no override → NOT match / NOT auto.
    assert result.stage_reached != d.AUTO_CLEARED
    assert result.needs_decision is True


def test_identical_floats_empty_ledger_auto_clears() -> None:
    env = d.reduce_envelope([], [], identity=_IDENT)
    diffs = d.diff_metrics({"widgets.rate": 1.0}, {"widgets.rate": 1.0})
    result = d.classify(diffs, env, current_scale="main", current_cluster="hoffman2")
    assert result.stage_reached == d.AUTO_CLEARED
    assert result.per_key[0].tier_reason == "exact"


# --- thin-vs-evidenced routing (BOTH directions) ----------------------------


def _thin_env() -> d.Envelope:
    # n=2 → thin. Range [1.0, 1.2].
    s1 = _sample([_pk("widgets.rate", 1.0, 1.0)])
    s2 = _sample([_pk("widgets.rate", 1.0, 1.2)])
    return d.reduce_envelope([s1, s2], [True, True], identity=_IDENT)


def _evidenced_env() -> d.Envelope:
    # n=3, same scale+cluster → well-evidenced. Range [1.0, 1.2].
    ss = [
        _sample([_pk("widgets.rate", 1.0, 1.0)]),
        _sample([_pk("widgets.rate", 1.0, 1.1)]),
        _sample([_pk("widgets.rate", 1.1, 1.2)]),
    ]
    return d.reduce_envelope(ss, [True, True, True], identity=_IDENT)


def test_inside_thin_routes_to_needs_verdict() -> None:
    diffs = d.diff_metrics({"widgets.rate": 1.05}, {"widgets.rate": 1.1})
    result = d.classify(diffs, _thin_env(), current_scale="canary", current_cluster="hoffman2")
    assert result.stage_reached == d.NEEDS_VERDICT
    assert result.per_key[0].tier_reason == "within_thin_envelope"


def test_outside_thin_routes_to_needs_verdict_not_mismatch() -> None:
    diffs = d.diff_metrics({"widgets.rate": 1.0}, {"widgets.rate": 5.0})
    result = d.classify(diffs, _thin_env(), current_scale="canary", current_cluster="hoffman2")
    assert result.stage_reached == d.NEEDS_VERDICT  # NOT mismatch — thin envelope
    assert result.per_key[0].tier_reason == "outside_thin_envelope"


def test_outside_evidenced_is_mismatch() -> None:
    diffs = d.diff_metrics({"widgets.rate": 1.0}, {"widgets.rate": 5.0})
    result = d.classify(diffs, _evidenced_env(), current_scale="canary", current_cluster="hoffman2")
    assert result.stage_reached == d.MISMATCH
    assert result.per_key[0].tier_reason == "outside_evidenced_envelope"


def test_inside_evidenced_auto_clears() -> None:
    diffs = d.diff_metrics({"widgets.rate": 1.05}, {"widgets.rate": 1.15})
    result = d.classify(diffs, _evidenced_env(), current_scale="canary", current_cluster="hoffman2")
    assert result.stage_reached == d.AUTO_CLEARED
    assert result.per_key[0].tier_reason == "within_evidenced_envelope"


def test_cluster_novelty_makes_evidenced_thin() -> None:
    # Same n=3 evidence, but the compared cluster is novel → NOT well-evidenced.
    diffs = d.diff_metrics({"widgets.rate": 1.05}, {"widgets.rate": 1.15})
    result = d.classify(diffs, _evidenced_env(), current_scale="canary", current_cluster="expanse")
    assert result.stage_reached == d.NEEDS_VERDICT
    assert result.per_key[0].tier_reason == "within_thin_envelope"


# --- identity-drift + data-identity staleness -------------------------------


def test_identity_drift_excluded_and_disclosed() -> None:
    current = _sample([_pk("widgets.rate", 1.0, 1.2)])
    stale = _sample(
        [_pk("widgets.rate", 9.0, 9.0)],
        identity={"cmd_sha": _CMD, "tasks_py_sha": "z" * 64, "executor": _EXEC},
    )
    env = d.reduce_envelope([current, stale], [True, True], identity=_IDENT)
    assert env.excluded_identity_drift == 1
    ke = env.per_key["widgets.rate"]
    assert ke.evidence.n == 1  # only the current-identity sample
    assert ke.hi == 1.2  # the stale 9.0 never entered the range


def test_data_identity_exclusion_disclosed() -> None:
    match = _sample(
        [_pk("widgets.rate", 1.0, 1.0)],
        identity={**_IDENT, "data_sha": "d1"},
    )
    drift = _sample(
        [_pk("widgets.rate", 9.0, 9.0)],
        identity={**_IDENT, "data_sha": "d2"},
    )
    unknown = _sample([_pk("widgets.rate", 1.0, 1.1)])  # no data_sha leg
    env = d.reduce_envelope(
        [match, drift, unknown], [True, True, True], identity=_IDENT, data_identity="d1"
    )
    assert env.excluded_data_drift == 1  # the d2 sample dropped
    assert env.data_identity_unknown == 1  # kept, disclosed, never blocking
    ke = env.per_key["widgets.rate"]
    assert ke.evidence.n == 2  # match + unknown
    assert ke.hi == 1.1  # the d2 9.0 excluded


# --- the admission gate (unadmitted never moves an envelope) -----------------


def test_unadmitted_sample_does_not_move_envelope() -> None:
    s = _sample([_pk("widgets.rate", 1.0, 5.0)])
    env = d.reduce_envelope([s], [False], identity=_IDENT)
    ke = env.per_key["widgets.rate"]
    assert ke.evidence.n == 0
    assert ke.lo is None and ke.hi is None
    assert ke.evidence.excluded_unadmitted == 1
    assert env.excluded_unadmitted == 1
    # evidence_meets counts admitted only → not met.
    met, shortfall = d.evidence_meets([s], [False], {"min_n": 1}, identity=_IDENT)
    assert met is False
    assert "min_n" in shortfall


def test_admitted_sample_moves_envelope_and_satisfies_demand() -> None:
    s = _sample([_pk("widgets.rate", 1.0, 5.0)])
    env = d.reduce_envelope([s], [True], identity=_IDENT)
    ke = env.per_key["widgets.rate"]
    assert ke.evidence.n == 1
    assert ke.lo == 1.0 and ke.hi == 5.0
    met, shortfall = d.evidence_meets([s], [True], {"min_n": 1}, identity=_IDENT)
    assert met is True
    assert shortfall == {}


# --- shape / key-set exact-class violations ---------------------------------


def test_shape_value_moved_is_mismatch() -> None:
    env = d.reduce_envelope([], [], identity=_IDENT)
    diffs = d.diff_metrics({"widgets.shape": [1, 2]}, {"widgets.shape": [1, 3]})
    assert diffs[0].static_class == "shape"
    result = d.classify(diffs, env, current_scale="main", current_cluster="hoffman2")
    assert result.stage_reached == d.MISMATCH
    assert result.per_key[0].tier_reason == "exact"


def test_key_set_change_is_incomparable_needs_verdict() -> None:
    env = d.reduce_envelope([], [], identity=_IDENT)
    diffs = d.diff_metrics({"widgets.a": 1}, {"widgets.b": 1})
    result = d.classify(diffs, env, current_scale="main", current_cluster="hoffman2")
    assert result.stage_reached == d.NEEDS_VERDICT
    assert all(kv.verdict == d.INCOMPARABLE for kv in result.per_key)


def test_int_key_moved_is_exact_class_mismatch() -> None:
    env = d.reduce_envelope([], [], identity=_IDENT)
    diffs = d.diff_metrics({"widgets.n": 3}, {"widgets.n": 4})
    result = d.classify(diffs, env, current_scale="main", current_cluster="hoffman2")
    assert result.stage_reached == d.MISMATCH
    assert result.per_key[0].tier_reason == "exact"


# --- caller-override labeling (precedence + disclosure) ----------------------


def test_caller_override_labeled_and_wins() -> None:
    # Even against a well-evidenced envelope that would say OUTSIDE, an owned
    # caller override wins and is labeled caller_override.
    diffs = d.diff_metrics({"widgets.rate": 1.0}, {"widgets.rate": 5.0})
    result = d.classify(
        diffs,
        _evidenced_env(),
        current_scale="canary",
        current_cluster="hoffman2",
        tolerance=lambda _k: (10.0, None),  # abs_tol 10 → the 4.0 diff matches
    )
    assert result.per_key[0].tier_reason == "caller_override"
    assert result.stage_reached == d.AUTO_CLEARED


def test_caller_override_mismatch_labeled() -> None:
    diffs = d.diff_metrics({"widgets.rate": 1.0}, {"widgets.rate": 5.0})
    result = d.classify(
        diffs,
        d.reduce_envelope([], [], identity=_IDENT),
        current_scale="main",
        current_cluster="hoffman2",
        tolerance=lambda _k: (0.1, None),  # too tight → mismatch
    )
    assert result.per_key[0].tier_reason == "caller_override"
    assert result.stage_reached == d.MISMATCH


# --- evidence_meets shortfalls + refusal ------------------------------------


def test_evidence_meets_shortfall_n() -> None:
    s = _sample([_pk("widgets.rate", 1.0, 1.0)])
    met, shortfall = d.evidence_meets([s], [True], {"min_n": 3}, identity=_IDENT)
    assert met is False
    assert shortfall["min_n"] == {"demanded": 3, "have": 1}


def test_evidence_meets_shortfall_n_full() -> None:
    s = _sample(
        [_pk("widgets.rate", 1.0, 1.0)],
        source="verify-reproduction",
        scale="main",
        partial=True,
        task_indices=[0],
    )
    met, shortfall = d.evidence_meets([s], [True], {"min_n": 1, "min_n_full": 1}, identity=_IDENT)
    assert met is False
    assert shortfall["min_n_full"] == {"demanded": 1, "have": 0}
    assert "min_n" not in shortfall  # n counts full+partial


def test_evidence_meets_shortfall_scale() -> None:
    s = _sample([_pk("widgets.rate", 1.0, 1.0)], scale="canary")
    met, shortfall = d.evidence_meets([s], [True], {"scales": ["main"]}, identity=_IDENT)
    assert met is False
    assert shortfall["scales"]["missing"] == ["main"]


def test_evidence_meets_shortfall_cluster() -> None:
    s = _sample([_pk("widgets.rate", 1.0, 1.0)], cluster="hoffman2")
    met, shortfall = d.evidence_meets([s], [True], {"clusters": ["expanse"]}, identity=_IDENT)
    assert met is False
    assert shortfall["clusters"]["missing"] == ["expanse"]


def test_evidence_meets_all_demands_met() -> None:
    ss = [
        _sample([_pk("widgets.rate", 1.0, 1.0)], scale="canary", cluster="hoffman2"),
        _sample(
            [_pk("widgets.rate", 1.0, 1.0)],
            source="verify-reproduction",
            scale="main",
            cluster="expanse",
        ),
    ]
    met, shortfall = d.evidence_meets(
        ss,
        [True, True],
        {"min_n": 2, "min_n_full": 1, "scales": ["canary", "main"], "clusters": ["hoffman2"]},
        identity=_IDENT,
    )
    assert met is True
    assert shortfall == {}


def test_evidence_meets_unknown_key_refused() -> None:
    s = _sample([_pk("widgets.rate", 1.0, 1.0)])
    with pytest.raises(errors.SpecInvalid):
        d.evidence_meets([s], [True], {"min_n": 1, "bogus": 2}, identity=_IDENT)


# --- AST / import pins -------------------------------------------------------


def test_no_statistics_import() -> None:
    src = inspect.getsource(d)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(a.name != "statistics" for a in node.names)
        if isinstance(node, ast.ImportFrom):
            assert node.module != "statistics"


def test_no_float_tolerance_literal_in_classifier() -> None:
    src = inspect.getsource(d)
    tree = ast.parse(src)
    classifier_fns = {
        "classify",
        "_classify_key",
        "_bucket",
        "_well_evidenced",
        "_inside_range",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in classifier_fns:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, float):
                    raise AssertionError(
                        f"float literal {sub.value!r} in classifier {node.name} "
                        "— core must invent no tolerance"
                    )


def test_validate_routes_through_attestation_kernel() -> None:
    # The one-kernel enforcement row: sample validation projects to the shared
    # attestation.validate, never a re-inlined shape check.
    src = inspect.getsource(d.validate_sample)
    assert "attestation.validate(" in src


# --- T1a: the ONE order-statistics leg (shared with conformance) -------------


def test_reduce_key_routes_through_shared_order_statistics_leg() -> None:
    # T1a: _reduce_key delegates min/max/spread to the ONE shared leg — the
    # fingerprint reduction and registration conformance's judge_window
    # (state/conformance.py) share ONE envelope definition (enforcement row),
    # never a second min/max/spread implementation.
    src = inspect.getsource(d._reduce_key)
    assert "order_statistics_envelope(" in src
    assert "min(values)" not in src and "max(values)" not in src


def test_order_statistics_envelope_byte_equal_to_reduction() -> None:
    # PURE-refactor pin: the shared leg reproduces the fingerprint reduction's
    # (lo, hi, rel_spread) EXACTLY over the same admitted values — byte-identical.
    samples = [
        _sample([_pk("widgets.rate", 0.94, 0.97)]),
        _sample([_pk("widgets.rate", 0.95, 0.96)]),
    ]
    env = d.reduce_envelope(samples, [True, True], identity=_IDENT)
    (key,) = env.per_key
    key_env = env.per_key[key]
    lo, hi, rel_spread = d.order_statistics_envelope([0.94, 0.97, 0.95, 0.96])
    assert (key_env.lo, key_env.hi, key_env.rel_spread) == (lo, hi, rel_spread)


def test_order_statistics_envelope_degenerate_scale_is_zero() -> None:
    # rel_spread is 0.0 when the magnitude scale is 0 (both endpoints 0) — the
    # no-invented-tolerance leg, not a divide-by-zero.
    assert d.order_statistics_envelope([0.0, 0.0]) == (0.0, 0.0, 0.0)
