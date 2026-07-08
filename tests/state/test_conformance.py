"""Tests for the live-conformance pure kernel (``state/conformance.py``).

Fixtures are the INSTRUMENT-QC toy ONLY (plan C6): a fake sensor ``sensor-7``,
calibration readings. NEVER trading vocabulary — real domain words in fixtures
smuggle a vocabulary into the tree that greps and future maintainers mistake for
core knowledge.
"""

from __future__ import annotations

import ast
import inspect
import math

import pytest

from hpc_agent import errors
from hpc_agent.state import conformance as cf
from hpc_agent.state import determinism

# --- toy instrument-QC fixtures ---------------------------------------------

_REG_ID = "sensor-7-calibration"
_DOSSIER_SHA = "a" * 64
_NOW = "2026-06-01T00:00:00+00:00"


def _baseline(readings):
    """Sealed calibration baseline: rows of {reading: value} for sensor-7."""
    return [{"reading": v} for v in readings]


def _receipt(value, *, observed_at, labels=None, key="reading"):
    """One live calibration receipt (a C-store observation record)."""
    return cf.build_observation_record(
        registration_id=_REG_ID,
        dossier_sha=_DOSSIER_SHA,
        status_at_record="current",
        payload={key: value},
        observed_at=observed_at,
        labels=labels or {"bench": "north"},
        emitter="sensor-7-daemon",
        ts=observed_at,
    )


def _stream(values, *, labels=None, start_day=1):
    """A homogeneous live window: one receipt per value, distinct timestamps."""
    return [
        _receipt(v, observed_at=f"2026-05-{start_day + i:02d}T00:00:00+00:00", labels=labels)
        for i, v in enumerate(values)
    ]


def _decl(*, keys=(), min_window_n=3, review_horizon=None):
    return cf.ConformanceDeclaration(
        baseline=cf.BaselineRef(path="calib/readings.json", sha256="b" * 64),
        keys=tuple(keys),
        min_window_n=min_window_n,
        review_horizon=review_horizon,
    )


def _reason(baseline, window, *, keys=(), min_window_n=3):
    """The per-(first-)key tier_reason for a judged window — shortens tests."""
    decl = _decl(keys=keys, min_window_n=min_window_n)
    return cf.judge_window(baseline, window, decl, now=_NOW).keys[0].tier_reason


# --- the fold: conforming / nonconforming / needs_verdict -------------------


def test_conforming_window_inside_well_evidenced_envelope():
    baseline = _baseline([0.94, 0.95, 0.96, 0.97, 0.93])  # envelope [0.93, 0.97]
    window = _stream([0.94, 0.95, 0.96])
    report = cf.judge_window(baseline, window, _decl(min_window_n=3), now=_NOW)
    assert report.tier == cf.CONFORMING
    assert [v.tier_reason for v in report.keys] == [cf.WITHIN_ENVELOPE]
    kv = report.keys[0]
    assert kv.within is True
    assert kv.baseline.lo == 0.93
    assert kv.baseline.hi == 0.97
    assert kv.baseline.n == 5
    assert kv.baseline.rel_spread == pytest.approx((0.97 - 0.93) / 0.97)
    assert kv.window_n == 3


def test_nonconforming_window_exits_well_evidenced_envelope():
    baseline = _baseline([0.94, 0.95, 0.96, 0.97, 0.93])
    window = _stream([0.94, 0.80, 0.95])  # 0.80 below the envelope floor
    report = cf.judge_window(baseline, window, _decl(min_window_n=3), now=_NOW)
    assert report.tier == cf.NONCONFORMING
    kv = report.keys[0]
    assert kv.tier_reason == cf.OUTSIDE_ENVELOPE
    assert kv.within is False


# --- insufficient window: BOTH directions route to needs_verdict ------------


def test_insufficient_window_inside_routes_needs_verdict():
    baseline = _baseline([0.94, 0.95, 0.96, 0.97, 0.93])
    window = _stream([0.95, 0.96])  # inside, but n=2 < min_window_n=5
    report = cf.judge_window(baseline, window, _decl(min_window_n=5), now=_NOW)
    assert report.tier == cf.NEEDS_VERDICT
    assert report.keys[0].tier_reason == cf.INSUFFICIENT_WINDOW
    assert report.keys[0].within is None


def test_insufficient_window_outside_routes_needs_verdict_not_nonconforming():
    baseline = _baseline([0.94, 0.95, 0.96, 0.97, 0.93])
    window = _stream([0.10, 0.20])  # wildly outside, but n=2 < min_window_n=5
    report = cf.judge_window(baseline, window, _decl(min_window_n=5), now=_NOW)
    # A verdict is never fabricated from evidence disclosed as insufficient.
    assert report.tier == cf.NEEDS_VERDICT
    assert report.keys[0].tier_reason == cf.INSUFFICIENT_WINDOW


# --- thin baseline -----------------------------------------------------------


def test_thin_baseline_never_auto_verdicts_even_when_outside():
    baseline = _baseline([0.95, 0.96])  # baseline_n=2 < 3 (well-evidenced bar)
    window = _stream([0.10, 0.20, 0.30])  # outside, but baseline is thin
    report = cf.judge_window(baseline, window, _decl(min_window_n=3), now=_NOW)
    assert report.tier == cf.NEEDS_VERDICT
    kv = report.keys[0]
    assert kv.tier_reason == cf.THIN_BASELINE
    assert kv.within is None


# --- key novelty -------------------------------------------------------------


def test_key_novelty_live_key_baseline_never_carried():
    baseline = _baseline([0.94, 0.95, 0.96])  # only 'reading'
    window = _stream([0.5, 0.6, 0.7], labels={"bench": "north"})
    # declare a key the baseline never carried
    report = cf.judge_window(baseline, window, _decl(keys=["humidity"], min_window_n=3), now=_NOW)
    assert report.tier == cf.NEEDS_VERDICT
    assert report.keys[0].tier_reason == cf.KEY_NOVELTY


def test_key_novelty_baseline_key_window_never_carried():
    baseline = _baseline([0.94, 0.95, 0.96])
    # window carries a different key than the baseline
    window = [
        _receipt(0.5, observed_at=f"2026-05-0{i}T00:00:00+00:00", key="drift") for i in range(1, 4)
    ]
    report = cf.judge_window(baseline, window, _decl(keys=["reading"], min_window_n=3), now=_NOW)
    assert report.tier == cf.NEEDS_VERDICT
    assert report.keys[0].tier_reason == cf.KEY_NOVELTY


# --- label novelty -----------------------------------------------------------


def test_label_novelty_heterogeneous_window():
    baseline = _baseline([0.93, 0.94, 0.95, 0.96, 0.97])
    window = [
        _receipt(0.94, observed_at="2026-05-01T00:00:00+00:00", labels={"bench": "north"}),
        _receipt(0.95, observed_at="2026-05-02T00:00:00+00:00", labels={"bench": "south"}),
        _receipt(0.96, observed_at="2026-05-03T00:00:00+00:00", labels={"bench": "north"}),
    ]
    report = cf.judge_window(baseline, window, _decl(min_window_n=3), now=_NOW)
    assert report.tier == cf.NEEDS_VERDICT
    kv = report.keys[0]
    assert kv.tier_reason == cf.LABEL_NOVELTY
    assert len(kv.label_sets) == 2  # both distinct label sets disclosed


# --- incomparable: NaN and type change --------------------------------------


def test_incomparable_nan_in_window():
    baseline = _baseline([0.93, 0.94, 0.95, 0.96, 0.97])
    window = _stream([0.94, math.nan, 0.95])
    report = cf.judge_window(baseline, window, _decl(min_window_n=3), now=_NOW)
    assert report.tier == cf.NEEDS_VERDICT
    assert report.keys[0].tier_reason == cf.INCOMPARABLE


def test_incomparable_type_change_string_value():
    baseline = _baseline([0.93, 0.94, 0.95, 0.96, 0.97])
    window = _stream([0.94, "out-of-range", 0.95])
    report = cf.judge_window(baseline, window, _decl(min_window_n=3), now=_NOW)
    assert report.tier == cf.NEEDS_VERDICT
    assert report.keys[0].tier_reason == cf.INCOMPARABLE


# --- every tier_reason fires (closed-vocabulary coverage) -------------------


def test_every_tier_reason_fires():
    ok = _baseline([0.93, 0.94, 0.95, 0.96, 0.97])
    het = [
        _receipt(0.94, observed_at="2026-05-01T00:00:00+00:00", labels={"bench": "north"}),
        _receipt(0.95, observed_at="2026-05-02T00:00:00+00:00", labels={"bench": "south"}),
        _receipt(0.96, observed_at="2026-05-03T00:00:00+00:00", labels={"bench": "north"}),
    ]
    fired = {
        _reason(ok, _stream([0.94, 0.95, 0.96])),  # within
        _reason(ok, _stream([0.10, 0.20, 0.30])),  # outside
        _reason(ok, _stream([0.95, 0.96]), min_window_n=5),  # insufficient
        _reason(_baseline([0.95, 0.96]), _stream([0.95, 0.96, 0.94])),  # thin baseline
        _reason(ok, _stream([0.5, 0.6, 0.7]), keys=["missing"]),  # key novelty
        _reason(ok, het),  # label novelty
        _reason(ok, _stream([0.94, math.nan, 0.95])),  # incomparable
    }
    assert fired == cf.TIER_REASONS


# --- the no-control-rules behavior pin --------------------------------------


def test_eight_consecutive_near_limit_inside_points_stay_conforming():
    # Envelope [0.90, 1.00]; 8 consecutive points hugging the upper limit (all
    # inside). A Western-Electric run-rule would flag this; core ships NO control
    # rules, so it is exactly 8 conforming reads.
    baseline = _baseline([0.90, 0.92, 0.95, 0.98, 1.00])
    window = _stream([0.995, 0.996, 0.997, 0.998, 0.999, 0.994, 0.993, 0.992])
    report = cf.judge_window(baseline, window, _decl(min_window_n=8), now=_NOW)
    assert report.tier == cf.CONFORMING
    assert report.keys[0].tier_reason == cf.WITHIN_ENVELOPE


# --- the sealed baseline: recording live receipts changes no envelope byte --


def test_sealed_baseline_no_admission_recording_changes_no_envelope():
    baseline = _baseline([0.93, 0.94, 0.95, 0.96, 0.97])
    r1 = cf.judge_window(baseline, _stream([0.94, 0.95, 0.96]), _decl(min_window_n=3), now=_NOW)
    # A totally different (drifted) live stream — the baseline input is what it
    # is; no live sample can widen it (no admission path exists).
    r2 = cf.judge_window(baseline, _stream([0.10, 0.20, 0.30]), _decl(min_window_n=3), now=_NOW)
    assert r1.keys[0].baseline == r2.keys[0].baseline  # byte-identical envelope
    # and there is no admission/re-baseline function on the module surface
    assert not any("admit" in name or "baseline_update" in name for name in dir(cf))


# --- route-through pin: ONE order-statistics helper -------------------------


def test_judge_window_routes_through_the_one_envelope_helper():
    # judge_window / _judge_key never re-inline min/max/spread — they delegate to
    # this module's thin alias, which itself routes through the ONE shared
    # order-statistics leg (state/determinism.py::order_statistics_envelope). The
    # one-envelope invariant (enforcement row): the fingerprint reduction and
    # judge_window share ONE definition — never a second min/max/spread.
    for fn in (cf.judge_window, cf._judge_key):
        src = inspect.getsource(fn)
        assert "min(" not in src and "max(" not in src, f"{fn.__name__} re-inlines order stats"
    helper_src = inspect.getsource(cf._order_statistics_envelope)
    # the alias owns no min/max/spread math of its own — it delegates
    assert "min(" not in helper_src and "max(" not in helper_src
    assert "determinism.order_statistics_envelope" in helper_src
    # the shared leg is the ONE place min/max/spread lives
    shared_src = inspect.getsource(determinism.order_statistics_envelope)
    assert "min(" in shared_src and "max(" in shared_src


def test_observation_routes_through_the_attestation_kernel():
    src = inspect.getsource(cf.to_attestation)
    assert "attestation.validate" in src


# --- AST pin: no numeric threshold literal beyond the n>=3 bar --------------


def test_no_numeric_threshold_literal_in_classifier():
    # The ONLY mechanized numeric threshold is WELL_EVIDENCED_MIN_N (=3), used by
    # NAME. The classifier body carries no bare numeric constant except the
    # structural 0/1 (indices, base cases). A "reasonable default" window or a
    # closeness tolerance landing here fails this pin.
    allowed = {0, 1}
    for fn in (cf.judge_window, cf._judge_key, cf._order_statistics_envelope):
        tree = ast.parse(inspect.getsource(fn).lstrip())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant):
                continue
            value = node.value
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            assert value in allowed, (
                f"{fn.__name__}: bare numeric literal {node.value!r} — the only "
                "mechanized threshold is WELL_EVIDENCED_MIN_N, used by name"
            )
    # the bar itself is the reused fingerprint value
    assert cf.WELL_EVIDENCED_MIN_N == 3


# --- declaration validator (C-declare) --------------------------------------


def test_declaration_valid_minimal():
    decl = cf.validate_declaration(
        {"baseline": {"path": "calib/readings.json", "sha256": "b" * 64}, "min_window_n": 20}
    )
    assert decl.baseline.path == "calib/readings.json"
    assert decl.keys == ()  # empty → all baseline keys (disclosed at judge time)
    assert decl.min_window_n == 20
    assert decl.review_horizon is None


def test_declaration_unknown_key_refused_loud():
    with pytest.raises(errors.SpecInvalid, match="unknown key"):
        cf.validate_declaration(
            {
                "baseline": {"path": "p", "sha256": "s"},
                "min_window_n": 5,
                "cadence_days": 90,  # an opted-in requirement core cannot check
            }
        )


def test_declaration_unknown_baseline_key_refused():
    with pytest.raises(errors.SpecInvalid, match="baseline unknown key"):
        cf.validate_declaration(
            {"baseline": {"path": "p", "sha256": "s", "url": "x"}, "min_window_n": 5}
        )


def test_declaration_min_window_n_must_be_positive_int():
    for bad in (0, -3, 2.5, "5", True):
        with pytest.raises(errors.SpecInvalid, match="min_window_n"):
            cf.validate_declaration({"baseline": {"path": "p", "sha256": "s"}, "min_window_n": bad})


def test_declaration_missing_baseline_refused():
    with pytest.raises(errors.SpecInvalid, match="baseline"):
        cf.validate_declaration({"min_window_n": 5})


def test_declaration_duplicate_key_refused():
    with pytest.raises(errors.SpecInvalid, match="duplicate key"):
        cf.validate_declaration(
            {"baseline": {"path": "p", "sha256": "s"}, "keys": ["a", "a"], "min_window_n": 5}
        )


# --- observation record model + validation (C-store) ------------------------


def test_build_and_validate_observation_roundtrip():
    rec = _receipt(0.95, observed_at=_NOW)
    obs = cf.validate_observation(rec)
    assert obs.registration_id == _REG_ID
    assert obs.status_at_record == "current"
    assert obs.content_sha == cf.canonical_content_sha(
        rec["payload"], rec["labels"], rec["observed_at"]
    )


def test_content_sha_is_deterministic_over_payload_labels_observed_at():
    a = cf.canonical_content_sha({"reading": 0.9}, {"bench": "north"}, _NOW)
    b = cf.canonical_content_sha({"reading": 0.9}, {"bench": "north"}, _NOW)
    c = cf.canonical_content_sha({"reading": 0.91}, {"bench": "north"}, _NOW)
    assert a == b and a != c


def test_validate_observation_refuses_wrong_attestor():
    rec = _receipt(0.95, observed_at=_NOW)
    rec["attestor"] = "human"
    with pytest.raises(errors.SpecInvalid):
        cf.validate_observation(rec)


def test_validate_observation_refuses_bad_status_at_record():
    rec = _receipt(0.95, observed_at=_NOW)
    rec["registration"]["status_at_record"] = "absent"  # not a recordable status
    with pytest.raises(errors.SpecInvalid, match="status_at_record"):
        cf.validate_observation(rec)


def test_validate_observation_refuses_registration_id_mismatch():
    rec = _receipt(0.95, observed_at=_NOW)
    rec["registration"]["registration_id"] = "other-sensor"
    with pytest.raises(errors.SpecInvalid, match="must equal subject_id"):
        cf.validate_observation(rec)


def test_status_at_record_vocabulary_excludes_absent():
    assert "absent" not in cf.STATUS_AT_RECORD
    assert sorted(cf.STATUS_AT_RECORD) == ["current", "revoked", "stale", "superseded"]


# --- window selection arithmetic (C-compare) --------------------------------


def test_select_window_last_n():
    receipts = _stream([0.1, 0.2, 0.3, 0.4, 0.5])
    picked = cf.select_window(receipts, last_n=2)
    assert [r["payload"]["reading"] for r in picked] == [0.4, 0.5]


def test_select_window_since_until():
    receipts = _stream([0.1, 0.2, 0.3, 0.4, 0.5])  # days 05-01 .. 05-05
    since = "2026-05-02T00:00:00+00:00"
    until = "2026-05-04T00:00:00+00:00"
    picked = cf.select_window(receipts, since=since, until=until)
    assert [r["payload"]["reading"] for r in picked] == [0.2, 0.3, 0.4]


def test_select_window_requires_a_mode():
    with pytest.raises(errors.SpecInvalid, match="window selection is required"):
        cf.select_window(_stream([0.1, 0.2]))


def test_select_window_refuses_mixed_modes():
    with pytest.raises(errors.SpecInvalid, match="never both"):
        cf.select_window(_stream([0.1, 0.2]), since=_NOW, last_n=2)


def test_select_window_bad_last_n_refused():
    for bad in (0, -1, 2.5):
        with pytest.raises(errors.SpecInvalid, match="last_n"):
            cf.select_window(_stream([0.1, 0.2]), last_n=bad)


def test_select_window_z_suffix_timestamp():
    receipts = [_receipt(0.5, observed_at="2026-05-03T00:00:00Z")]
    picked = cf.select_window(receipts, since="2026-05-02T00:00:00Z")
    assert len(picked) == 1


# --- report disclosure -------------------------------------------------------


def test_report_threads_now_as_of_and_baseline_key_disclosure():
    baseline = _baseline([0.93, 0.94, 0.95, 0.96, 0.97])
    report = cf.judge_window(baseline, _stream([0.94, 0.95, 0.96]), _decl(min_window_n=3), now=_NOW)
    assert report.as_of == _NOW
    assert report.keys_from_baseline is True  # empty declaration → baseline keys
    assert [v.key for v in report.keys] == ["reading"]


def test_declared_keys_not_from_baseline_flag():
    baseline = _baseline([0.93, 0.94, 0.95, 0.96, 0.97])
    decl = _decl(keys=["reading"], min_window_n=3)
    report = cf.judge_window(baseline, _stream([0.94, 0.95, 0.96]), decl, now=_NOW)
    assert report.keys_from_baseline is False
