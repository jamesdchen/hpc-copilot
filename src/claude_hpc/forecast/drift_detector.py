"""Track training-quality metrics across runs to detect model drift.

The trainer (``scripts/train_wait_predictor.py``) writes a fresh
``training_summary.json`` on every run. This module appends each
summary to a rolling history and surfaces a "drift signal" when the
recent validation MAE departs significantly from the recent baseline.

Cluster behavior shifts are real: new accounts, partition reconfig,
SLURM upgrades, semester transitions all change the model's input
distribution. Without drift detection a model trained against last
quarter's queue silently mis-predicts forever.

Detection rule (deliberately simple):

* Compare the **most recent** ``val_mae_sec`` to the **median** of
  the prior K runs (default 5).
* If recent > 1.5x median → ``mae_regression`` drift.
* If recent < 0.66x median → ``mae_improvement`` (also worth noting;
  could indicate label leakage, accidentally testing on training).

Both signals are advisory. The detector returns the diagnosis; the
caller decides whether to retrain, alert, or ignore.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any, Literal

DriftStatus = Literal[
    "ok",
    "insufficient_history",
    "mae_regression",
    "mae_improvement",
]


@dataclass(frozen=True)
class DriftDiagnosis:
    """Output of :func:`diagnose_drift`."""

    status: DriftStatus
    recent_mae_sec: float | None
    baseline_median_mae_sec: float | None
    ratio: float | None  # recent / baseline; None when either is missing
    n_history: int


def history_path(experiment_dir: Path) -> Path:
    return experiment_dir / ".hpc" / "wait_predictor" / "training_history.jsonl"


def append_run(experiment_dir: Path, *, summary: dict[str, Any]) -> Path:
    """Append a training-run summary as one JSONL line.

    Idempotent on accidental duplicate calls (we don't dedupe; the
    caller controls invocation cadence). Returns the path written.
    """
    path = history_path(experiment_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(summary, sort_keys=True) + "\n")
    return path


def read_history(
    experiment_dir: Path,
    *,
    keep_last: int | None = None,
) -> list[dict[str, Any]]:
    """Load the JSONL history (oldest first); optionally trim to the
    most recent *keep_last* runs."""
    path = history_path(experiment_dir)
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if keep_last is not None:
        rows = rows[-keep_last:]
    return rows


def diagnose_drift(
    experiment_dir: Path,
    *,
    min_baseline_runs: int = 5,
    regression_ratio: float = 1.5,
    improvement_ratio: float = 0.66,
) -> DriftDiagnosis:
    """Compare the most-recent run's val_mae against the median of
    the prior *min_baseline_runs* runs."""
    history = read_history(experiment_dir)
    if len(history) < min_baseline_runs + 1:
        return DriftDiagnosis(
            status="insufficient_history",
            recent_mae_sec=history[-1].get("val_mae_sec") if history else None,
            baseline_median_mae_sec=None,
            ratio=None,
            n_history=len(history),
        )
    recent = history[-1].get("val_mae_sec")
    baseline_runs = history[-(min_baseline_runs + 1) : -1]
    baseline_maes = [r["val_mae_sec"] for r in baseline_runs if "val_mae_sec" in r]
    if recent is None or not baseline_maes:
        return DriftDiagnosis(
            status="insufficient_history",
            recent_mae_sec=recent,
            baseline_median_mae_sec=None,
            ratio=None,
            n_history=len(history),
        )
    baseline = median(baseline_maes)
    if baseline <= 0:
        return DriftDiagnosis(
            status="ok",
            recent_mae_sec=recent,
            baseline_median_mae_sec=baseline,
            ratio=None,
            n_history=len(history),
        )
    ratio = recent / baseline
    if ratio > regression_ratio:
        status: DriftStatus = "mae_regression"
    elif ratio < improvement_ratio:
        status = "mae_improvement"
    else:
        status = "ok"
    return DriftDiagnosis(
        status=status,
        recent_mae_sec=recent,
        baseline_median_mae_sec=baseline,
        ratio=ratio,
        n_history=len(history),
    )


__all__ = ["DriftDiagnosis", "DriftStatus", "append_run", "diagnose_drift", "read_history"]
