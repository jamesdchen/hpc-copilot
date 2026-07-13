"""T10 — the determinism-fingerprint ENFORCEMENT suite (the boundary contract).

Design origin: ``docs/design/determinism-fingerprint.md`` — the "Enforcement rows"
table. Each test here is the named holder of one row (the
``docs/internals/engineering-principles.md`` map points at these functions). Where
a row also has a pure-kernel unit test in ``tests/state/test_determinism.py``, this
suite states the row at the CONTRACT level (behavior + AST/route-through pins that
span modules).

TOY-DOMAIN fixtures ONLY — the widget lineage (``widget.jam`` metrics, a
``widgetize.py`` executor). Never harxhar/quant vocabulary: real domain words in a
fixture smuggle a vocabulary into the tree (the domain-packs toy-fixture rule).
"""

from __future__ import annotations

import ast
import inspect
import math
from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent.state import determinism, fingerprint_store
from hpc_agent.state.decision_journal import append_decision

if TYPE_CHECKING:
    from pathlib import Path

# ── toy widget fixtures ──────────────────────────────────────────────────────

_KEY = "widget.jam"
_ID = {"cmd_sha": "widget-cmd", "tasks_py_sha": "widget-code", "executor": "widgetize.py"}


def _sample_record(
    *,
    a: float,
    b: float,
    scale: str = "canary",
    cluster: str = "widget-lab",
    verdict: str = "auto_cleared",
    content_sha: str = "ab" * 32,
    run_ids: tuple[str, str] = ("r-orig", "r-repro"),
    partial: bool = False,
    task_indices: list[int] | None = None,
) -> dict:
    """A valid D-store sample DICT over the toy widget metric (never validated harness)."""
    diffs = determinism.diff_metrics({_KEY: a}, {_KEY: b})
    return determinism.build_sample_record(
        ts="2026-01-01T00:00:00Z",
        content_sha=content_sha,
        identity=dict(_ID),
        source="double-canary" if scale == "canary" else "verify-reproduction",
        run_ids=list(run_ids),
        cluster=cluster,
        scale=scale,
        verdict=verdict,
        per_key=diffs,
        same_submission=(scale == "canary"),
        partial=partial,
        task_indices=task_indices,
    )


def _sample(**kwargs) -> determinism.Sample:
    """Same as :func:`_sample_record`, validated to a :class:`Sample` for the kernel."""
    return determinism.validate_sample(_sample_record(**kwargs))


def _diff(a: float, b: float) -> list[determinism.PerKeyDiff]:
    return determinism.diff_metrics({_KEY: a}, {_KEY: b})


# ── row: NO INVENTED TOLERANCE ───────────────────────────────────────────────


def test_last_ulp_floats_empty_ledger_not_match() -> None:
    """Two floats differing in the LAST ULP, an empty ledger, no override → NOT match.

    Absent measured evidence and a caller override, every comparison is EXACT — core
    carries no default float tolerance, so even a 1-ulp deviation does not auto-clear.
    """
    a = 1.0
    b = math.nextafter(1.0, 2.0)  # the smallest possible float above 1.0
    assert a != b
    env = determinism.reduce_envelope([], [], identity=_ID)  # empty ledger
    result = determinism.classify(
        _diff(a, b), env, current_scale="main", current_cluster="widget-lab"
    )
    assert result.per_key[0].verdict != "match"
    assert result.stage_reached != determinism.AUTO_CLEARED


def test_no_numeric_tolerance_literal_in_classifier() -> None:
    """AST pin: the classifier carries NO numeric tolerance literal.

    A "reasonable default" epsilon landing anywhere in the per-key classifier is the
    invented-tolerance failure. The only numeric constant tolerated is ``0`` (the
    identity check ``abs_diff == 0``); any other int/float literal fails this pin.
    """
    src = inspect.getsource(determinism._classify_key)
    offenders = [
        node.value
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
        and node.value != 0
    ]
    assert offenders == [], f"a numeric tolerance literal leaked into the classifier: {offenders}"


# ── row: ORDER STATISTICS ONLY ───────────────────────────────────────────────


def test_envelope_is_observed_range_never_fitted() -> None:
    """The n=2 envelope is exactly the observed min/max — no mean, no widening."""
    priors = [_sample(a=1.00, b=1.02), _sample(a=1.01, b=1.03)]
    env = determinism.reduce_envelope(priors, [True, True], identity=_ID)
    ke = env.per_key[_KEY]
    assert ke.lo == 1.00 and ke.hi == 1.03  # exact order statistics over {1.00,1.02,1.01,1.03}
    assert ke.cls == determinism.STOCHASTIC
    assert ke.rel_spread == (1.03 - 1.00) / 1.03  # derived from the range, not fitted


def test_no_statistics_or_variance_in_envelope_path() -> None:
    """No ``statistics``/variance/numpy import can back the envelope reduction."""
    src = inspect.getsource(determinism)
    assert "import statistics" not in src
    assert "statistics." not in src
    assert "import numpy" not in src
    for banned in ("stdev", "variance", "pstdev", "mean("):
        assert banned not in src, f"a fitted-distribution primitive leaked in: {banned!r}"


# ── row: THIN NEVER AUTO (both directions) ───────────────────────────────────


def _thin_env() -> determinism.Envelope:
    # n=2 stochastic envelope over [1.0, 1.1] — thin (n < 3).
    priors = [_sample(a=1.0, b=1.1, scale="main"), _sample(a=1.0, b=1.1, scale="main")]
    return determinism.reduce_envelope(priors, [True, True], identity=_ID)


def test_thin_envelope_inside_routes_to_needs_verdict() -> None:
    env = _thin_env()
    result = determinism.classify(
        _diff(1.02, 1.05), env, current_scale="main", current_cluster="widget-lab"
    )
    assert result.stage_reached == determinism.NEEDS_VERDICT
    assert result.per_key[0].tier_reason == "within_thin_envelope"


def test_thin_envelope_outside_routes_to_needs_verdict_not_mismatch() -> None:
    env = _thin_env()
    result = determinism.classify(
        _diff(1.0, 2.0), env, current_scale="main", current_cluster="widget-lab"
    )
    # Outside a THIN envelope is a HUMAN question, never a machine mismatch.
    assert result.stage_reached == determinism.NEEDS_VERDICT
    assert result.per_key[0].tier_reason == "outside_thin_envelope"


# ── row: ONE ADMISSION RULE, SCOPED TO THE ENVELOPE ──────────────────────────


def test_unadmitted_sample_does_not_move_envelope() -> None:
    s = _sample(a=1.0, b=1.5, scale="main")
    admitted = determinism.reduce_envelope([s], [True], identity=_ID)
    unadmitted = determinism.reduce_envelope([s], [False], identity=_ID)
    assert admitted.per_key[_KEY].cls == determinism.STOCHASTIC  # admitted spreads the range
    # Unadmitted: the key is disclosed but contributes NOTHING to the range.
    assert unadmitted.per_key[_KEY].evidence.n == 0
    assert unadmitted.per_key[_KEY].lo is None and unadmitted.per_key[_KEY].hi is None
    assert unadmitted.excluded_unadmitted == 1


def test_admitted_satisfies_demand_unadmitted_never() -> None:
    s = _sample(a=1.0, b=1.0, scale="main")
    met, _ = determinism.evidence_meets([s], [True], {"min_n": 1}, identity=_ID)
    assert met
    unmet, shortfall = determinism.evidence_meets([s], [False], {"min_n": 1}, identity=_ID)
    assert not unmet and "min_n" in shortfall


def test_double_canary_prior_admitted_by_construction(tmp_path: Path) -> None:
    """A ``double-canary`` sample (``verdict=auto_cleared``) admits with NO journal join."""
    record = _sample_record(a=1.0, b=1.0, scale="canary", verdict="auto_cleared")
    flags, excluded = fingerprint_store.compute_admitted_flags(tmp_path, [record])
    assert flags == [True] and excluded == 0


# ── row: CODE ATTESTATION NEVER SATISFIES A HUMAN TIER ────────────────────────


def test_mismatch_admits_only_via_human_record_code_cannot_substitute(tmp_path: Path) -> None:
    """A ``mismatch`` sample is inadmissible until a GATED human acceptance names it.

    Code observing nondeterminism (the ``mismatch`` verdict) never launders itself in;
    admission requires the human ``reproduction-verdict`` record (the human tier) —
    a code attestation cannot stand in for it.
    """
    sha = "e" * 64
    record = _sample_record(
        a=1.0, b=1.5, scale="main", verdict="mismatch", content_sha=sha, run_ids=("o-x", "r-x")
    )
    flags, excluded = fingerprint_store.compute_admitted_flags(tmp_path, [record])
    assert flags == [False] and excluded == 1  # no human record yet
    # The human accepts it on the repro run scope (block reproduction-verdict, gated).
    append_decision(
        tmp_path,
        scope_kind="run",
        scope_id="r-x",
        block="reproduction-verdict",
        response=f"accept {sha[:8]}",
        resolved={"accept": True, "content_sha": sha},
    )
    flags, excluded = fingerprint_store.compute_admitted_flags(tmp_path, [record])
    assert flags == [True] and excluded == 0


# ── row: NO-SILENT-CAPS ON PARTIALITY ────────────────────────────────────────


def test_partial_sample_without_indices_refused() -> None:
    with pytest.raises(errors.SpecInvalid, match="task_indices"):
        _sample_record(a=1.0, b=1.0, partial=True, task_indices=None)


def test_partial_receipt_missing_uncompared_accounting_refused() -> None:
    from hpc_agent.ops.verify_reproduction import _validate_receipt_partiality

    # task_indices present but the uncompared accounting is null → refused at append.
    with pytest.raises(errors.SpecInvalid, match="uncompared"):
        _validate_receipt_partiality(
            {"partial": True, "task_indices": [0], "uncompared_keys": None, "uncompared_tasks": 1}
        )


# ── row: -CANARY2 EXCLUSION ROUTES THROUGH _sibling_run_ids ───────────────────


def test_canary2_exclusion_routes_through_the_one_sibling_definition() -> None:
    """The aggregate reduce excludes the ``-canary`` FAMILY via the ONE suffix
    definition (``ops/monitor/reconcile.py::_sibling_run_ids``), never a literal
    ``-canary2``. The planted-``-canary2``-row fire test lives in T4's aggregate suite
    (``tests/ops/aggregate/test_flow_ssh_default_reducer.py::
    test_ssh_fallback_excludes_canary2_sibling_results``) — not duplicated here."""
    from hpc_agent.ops import aggregate_flow
    from hpc_agent.ops.monitor import reconcile

    src = inspect.getsource(aggregate_flow._per_task_metrics_reduce)
    # Route-through: the reduce calls the ONE suffix definition, never a re-inlined
    # ``result_dirs = [d for d in ... if "-canary" not in d]`` literal filter.
    assert "_sibling_run_ids" in src
    assert 'family.append(f"{run_id}-canary")' not in src  # the family lives in reconcile, not here
    # The ONE definition covers the whole ``-canary`` family, ``-canary2`` included.
    assert set(reconcile._sibling_run_ids("main")) == {"main-canary", "main-canary2"}


# ── row: NO VERDICT VERB ─────────────────────────────────────────────────────


def test_no_verdict_verb_in_registry() -> None:
    """The needs_verdict resolution is ``append-decision`` (block reproduction-verdict)
    or nothing — NO primitive resolves/mutates a reproduction verdict (the
    no-unlock-verb doctrine). Mirrors ``test_no_utterance_writing_verb_in_registry``."""
    from tests._registry_helpers import core_only_registry

    offenders = [
        name
        for name in core_only_registry()
        if "resolve-reproduction" in name
        or ("reproduction" in name and "verdict" in name)
        or name == "resolve-verdict"
    ]
    assert offenders == [], (
        f"a reproduction-verdict-writing verb leaked into the registry: {offenders}"
    )


# ── row: PRECEDENCE — caller (labeled) > measured > exact ─────────────────────


def test_precedence_caller_over_measured_over_exact() -> None:
    diffs = _diff(1.0, 1.05)  # a 0.05 nonzero float deviation

    # EXACT tier — empty ledger, no override: no invented tolerance → NOT a match.
    exact_env = determinism.reduce_envelope([], [], identity=_ID)
    r_exact = determinism.classify(
        diffs, exact_env, current_scale="main", current_cluster="widget-lab"
    )
    assert r_exact.per_key[0].verdict == "mismatch"
    assert r_exact.per_key[0].tier_reason == "outside_thin_envelope"

    # MEASURED tier — a well-evidenced envelope [1.0, 1.1], no override: inside → match.
    priors = [_sample(a=1.0, b=1.1, scale="main") for _ in range(3)]
    env = determinism.reduce_envelope(priors, [True, True, True], identity=_ID)
    r_meas = determinism.classify(diffs, env, current_scale="main", current_cluster="widget-lab")
    assert r_meas.per_key[0].verdict == "match"
    assert r_meas.per_key[0].tier_reason == "within_evidenced_envelope"

    # CALLER tier — an owned override outranks the measurement AND is DISCLOSED.
    def tol(_key: str) -> tuple[float | None, float | None]:
        return (0.01, None)  # abs_tol 0.01: the 0.05 deviation FAILS the owned tolerance

    r_call = determinism.classify(
        diffs, env, current_scale="main", current_cluster="widget-lab", tolerance=tol
    )
    assert r_call.per_key[0].tier_reason == "caller_override"  # labeled, never silent
    assert r_call.per_key[0].verdict == "mismatch"  # the caller owns it, overriding "inside"
