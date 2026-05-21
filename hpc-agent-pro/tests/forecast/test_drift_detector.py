"""Tests for ``forecast.drift_detector``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent_pro.forecast.drift_detector import (
    append_run,
    diagnose_drift,
    history_path,
    read_history,
)

if TYPE_CHECKING:
    from pathlib import Path


def _seed(tmp_path: Path, *maes: float) -> None:
    """Append a sequence of training-run summaries with given val_mae."""
    for mae in maes:
        append_run(tmp_path, summary={"val_mae_sec": mae})


# ─── append + read ────────────────────────────────────────────────────


def test_history_path_is_predictable(tmp_path: Path) -> None:
    assert history_path(tmp_path) == tmp_path / ".hpc" / "wait_predictor" / "training_history.jsonl"


def test_append_creates_directory_and_file(tmp_path: Path) -> None:
    append_run(tmp_path, summary={"val_mae_sec": 100.0, "n_train": 50})
    rows = read_history(tmp_path)
    assert len(rows) == 1
    assert rows[0]["val_mae_sec"] == 100.0
    assert rows[0]["n_train"] == 50


def test_append_multiple_rounds_reads_all(tmp_path: Path) -> None:
    _seed(tmp_path, 100.0, 110.0, 120.0)
    rows = read_history(tmp_path)
    assert [r["val_mae_sec"] for r in rows] == [100.0, 110.0, 120.0]


def test_keep_last_trims_history(tmp_path: Path) -> None:
    _seed(tmp_path, 100.0, 110.0, 120.0, 130.0)
    rows = read_history(tmp_path, keep_last=2)
    assert [r["val_mae_sec"] for r in rows] == [120.0, 130.0]


def test_corrupt_lines_skipped(tmp_path: Path) -> None:
    _seed(tmp_path, 100.0)
    # Hand-corrupt the file with a garbage line in the middle.
    p = history_path(tmp_path)
    p.write_text(p.read_text() + "{not json\n" + '{"val_mae_sec": 200.0}\n')
    rows = read_history(tmp_path)
    assert [r["val_mae_sec"] for r in rows] == [100.0, 200.0]


# ─── diagnose_drift ────────────────────────────────────────────────────


def test_insufficient_history_returns_dedicated_status(tmp_path: Path) -> None:
    """Need ``min_baseline_runs + 1`` runs before drift is computable."""
    _seed(tmp_path, 100.0, 100.0)  # only 2 runs
    out = diagnose_drift(tmp_path, min_baseline_runs=5)
    assert out.status == "insufficient_history"
    assert out.recent_mae_sec == 100.0
    assert out.baseline_median_mae_sec is None


def test_steady_mae_is_ok(tmp_path: Path) -> None:
    """All recent runs around 100s — no drift."""
    _seed(tmp_path, 100.0, 105.0, 95.0, 100.0, 102.0, 103.0)
    out = diagnose_drift(tmp_path, min_baseline_runs=5)
    assert out.status == "ok"
    assert out.ratio is not None
    assert 0.9 < out.ratio < 1.1


def test_mae_jump_flagged_as_regression(tmp_path: Path) -> None:
    """Recent run is 2x the baseline median → mae_regression."""
    _seed(tmp_path, 100.0, 100.0, 100.0, 100.0, 100.0, 200.0)
    out = diagnose_drift(tmp_path, min_baseline_runs=5)
    assert out.status == "mae_regression"
    assert out.ratio == 2.0


def test_mae_drop_flagged_as_improvement(tmp_path: Path) -> None:
    """Recent run is half the baseline → mae_improvement (worth
    investigating; could be label leakage)."""
    _seed(tmp_path, 100.0, 100.0, 100.0, 100.0, 100.0, 50.0)
    out = diagnose_drift(tmp_path, min_baseline_runs=5)
    assert out.status == "mae_improvement"
    assert out.ratio == 0.5


def test_zero_baseline_returns_ok_to_avoid_div_by_zero(tmp_path: Path) -> None:
    """Defensive: if baseline median is 0, can't compute ratio."""
    _seed(tmp_path, 0.0, 0.0, 0.0, 0.0, 0.0, 100.0)
    out = diagnose_drift(tmp_path, min_baseline_runs=5)
    assert out.status == "ok"
    assert out.ratio is None
