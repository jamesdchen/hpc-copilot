"""Tests for ``scripts/measure_dispatch_floor.py``.

The statistics / report assembly are unit-tested with INJECTED timings — no real
subprocess spawns run in CI. One ``@pytest.mark.slow`` smoke actually spawns the
two cheapest surfaces (bare interpreter, ``import hpc_agent``) once each and
asserts sane bounds.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "measure_dispatch_floor.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("measure_dispatch_floor", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mdf = _load_module()


# ── summarize ────────────────────────────────────────────────────────────────
def test_summarize_basic_stats():
    # seconds in → milliseconds out; median/min/max/mean computed on ms.
    out = mdf.summarize([0.010, 0.020, 0.030])
    assert out["n"] == 3
    assert out["median_ms"] == 20.0
    assert out["min_ms"] == 10.0
    assert out["max_ms"] == 30.0
    assert out["mean_ms"] == 20.0
    assert out["samples_ms"] == [10.0, 20.0, 30.0]


def test_summarize_even_count_median_is_mean_of_middle_two():
    out = mdf.summarize([0.010, 0.020, 0.030, 0.050])
    assert out["median_ms"] == 25.0  # (20+30)/2


def test_summarize_empty():
    out = mdf.summarize([])
    assert out["n"] == 0
    assert out["median_ms"] is None
    assert out["samples_ms"] == []


# ── compute_decision ─────────────────────────────────────────────────────────
def _summaries(**medians):
    """Build a minimal summaries dict where each surface has the given median."""
    keys = ("bare", "bare2", "import", "fast_path", "full_walk", "hook", "warm")
    out = {}
    for k in keys:
        m = medians.get(k)
        out[k] = {"median_ms": m}
    return out


def test_compute_decision_large_gap_reads_gap_remains():
    s = _summaries(bare=30.0, bare2=34.0, fast_path=320.0, full_walk=650.0, hook=90.0, warm=18.0)
    d = mdf.compute_decision(s)
    assert d["cold_fast_path_median_ms"] == 320.0
    assert d["warm_reference_measured_ms"] == 18.0
    assert d["per_turn_hook_cost_ms"] == pytest.approx(270.0)  # 3 × 90
    assert d["hooks_per_turn"] == mdf.HOOKS_PER_TURN
    assert d["spawn_variance_bare_vs_bare2_ms"] == pytest.approx(4.0)
    # 320 - 20 (band high) = 300
    assert d["residual_gap_vs_warm_band_ms"] == pytest.approx(300.0)
    assert d["residual_gap_vs_measured_warm_ms"] == pytest.approx(302.0)
    assert "gap remains" in d["reading"].lower()
    assert "does not rule" in d["ruling_ref"].lower()


def test_compute_decision_small_gap_leans_shelve():
    # cold floor within 1.5× the 20ms band high → SHELVE reading.
    s = _summaries(bare=10.0, bare2=11.0, fast_path=25.0, full_walk=40.0, hook=12.0, warm=16.0)
    d = mdf.compute_decision(s)
    assert "shelve" in d["reading"].lower()


def test_compute_decision_handles_missing_fast_path():
    s = _summaries(bare=10.0, bare2=11.0, hook=12.0, warm=16.0)
    d = mdf.compute_decision(s)
    assert d["cold_fast_path_median_ms"] is None
    assert d["per_turn_hook_cost_ms"] == pytest.approx(36.0)
    assert "cannot read" in d["reading"].lower()


# ── build_report (with injected raw timings, no spawns) ──────────────────────
def _fake_config(tmp_path, monkeypatch):
    ns = mdf.parse_args(["--runs", "3"])
    cfg = mdf.build_config(ns)
    cfg.out_path = tmp_path / "report.json"
    # Deterministic git state so the report shape is stable in CI.
    monkeypatch.setattr(
        mdf, "git_state", lambda _root: {"head": "deadbeef", "dirty": False, "dirty_line_count": 0}
    )
    monkeypatch.setattr(mdf, "wheel_version", lambda: "0.0.0-test")
    return cfg


def test_build_report_shape_and_decision(tmp_path, monkeypatch):
    cfg = _fake_config(tmp_path, monkeypatch)
    raw = {
        "bare": [0.030, 0.031, 0.029],
        "bare2": [0.032, 0.033, 0.031],
        "import": [0.120, 0.121, 0.119],
        "fast_path": [0.300, 0.310, 0.305],
        "full_walk": [0.640, 0.650, 0.645],
        "hook": [0.090, 0.091, 0.089],
        "warm": [0.017, 0.018, 0.019],
    }
    report = mdf.build_report(cfg, raw)
    assert report["schema"] == "hpc.measure_dispatch_floor.v1"
    assert report["runs_per_surface"] == 3
    assert report["git"]["head"] == "deadbeef"
    assert report["env"]["wheel_version"] == "0.0.0-test"
    assert report["boot_state"] == "warm-uncontrolled"
    assert report["first_run_of_boot"] is None
    # every surface present with a median
    for key in (*mdf.SUBPROCESS_KEYS, "warm"):
        assert report["surfaces"][key]["median_ms"] is not None
    assert report["surfaces"]["hook"]["caveat"]
    d = report["decision"]
    assert d["cold_fast_path_median_ms"] == 305.0
    assert d["warm_reference_measured_ms"] == 18.0


def test_build_report_cold_claim_labels_boot_state(tmp_path, monkeypatch):
    cfg = _fake_config(tmp_path, monkeypatch)
    cfg.cold_claim = True
    report = mdf.build_report(cfg, {k: [0.01] for k in (*mdf.SUBPROCESS_KEYS, "warm")})
    assert report["boot_state"] == "user-asserted-cold"


def test_render_table_flags_dirty_tree(tmp_path, monkeypatch):
    cfg = _fake_config(tmp_path, monkeypatch)
    monkeypatch.setattr(
        mdf, "git_state", lambda _root: {"head": "cafef00d", "dirty": True, "dirty_line_count": 5}
    )
    report = mdf.build_report(cfg, {k: [0.01, 0.02, 0.03] for k in (*mdf.SUBPROCESS_KEYS, "warm")})
    table = mdf.render_table(report)
    assert "WARNING" in table
    assert "DIRTY" in table
    assert "DECISION LINE" in table


# ── collect_samples (injected runners, no spawns) ────────────────────────────
def test_collect_samples_interleaves_and_counts(tmp_path):
    ns = mdf.parse_args(["--runs", "4"])
    cfg = mdf.build_config(ns)
    seen_order: list[str] = []

    def fake_surface(key, _cfg):
        seen_order.append(key)
        return 0.001

    def fake_warm(_argv):
        return 0.002

    raw = mdf.collect_samples(cfg, surface_runner=fake_surface, warm_runner=fake_warm)
    for key in mdf.SUBPROCESS_KEYS:
        if key == "full_walk":
            # Advisory surface: capped at the default --full-runs (3), not --runs.
            assert len(raw[key]) == 3
        else:
            assert len(raw[key]) == 4
    assert len(raw["warm"]) == 4
    # Interleave: the first surface of round 0 differs from the first of the
    # next round (rotation); full_walk's cap drops samples, not the rotation.
    assert seen_order[0] != seen_order[len(mdf.SUBPROCESS_KEYS)]


def test_collect_samples_full_runs_cap(tmp_path):
    """--full-runs caps only full_walk; a cap above --runs is a no-op."""
    calls: dict[str, int] = {}

    def fake_surface(key, _cfg):
        calls[key] = calls.get(key, 0) + 1
        return 0.001

    ns = mdf.parse_args(["--runs", "5", "--full-runs", "2"])
    cfg = mdf.build_config(ns)
    raw = mdf.collect_samples(cfg, surface_runner=fake_surface, warm_runner=lambda _a: 0.002)
    assert len(raw["full_walk"]) == 2
    assert calls["full_walk"] == 2  # capped calls, not discarded samples
    assert all(len(raw[k]) == 5 for k in mdf.SUBPROCESS_KEYS if k != "full_walk")

    ns = mdf.parse_args(["--runs", "2", "--full-runs", "9"])
    cfg = mdf.build_config(ns)
    raw = mdf.collect_samples(cfg, surface_runner=fake_surface, warm_runner=lambda _a: 0.002)
    assert len(raw["full_walk"]) == 2  # effective max is --runs


# ── slow smoke: real spawns of the two cheapest surfaces ─────────────────────
@pytest.mark.slow
def test_smoke_real_spawn_bounds():
    ns = mdf.parse_args(["--runs", "1"])
    cfg = mdf.build_config(ns)
    bare = mdf.run_surface_once("bare", cfg)
    imp = mdf.run_surface_once("import", cfg)
    for label, val in (("bare", bare), ("import", imp)):
        assert val > 0, f"{label} elapsed must be positive"
        assert val < 60, f"{label} elapsed {val}s unreasonably large"
