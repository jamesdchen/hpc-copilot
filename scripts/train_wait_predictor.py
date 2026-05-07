"""LightGBM trainer for the queue-wait residual regression.

Walks ``<experiment_dir>/.hpc/squeue_snapshots/`` (written by
``snapshot_squeue.py``) and a sacct history of completed jobs to
produce ``(features, observed_overhead)`` training rows. Fits a
LightGBM regressor on the rows; persists the model + the feature
list + a summary of training quality to
``<experiment_dir>/.hpc/wait_predictor/``.

Run periodically (e.g. nightly cron). The model file is loaded by
:func:`claude_hpc.forecast.predict_start.predict_start_time` at
inference time; updates to the model take effect on the next
forecast call.

Optional dep: ``lightgbm`` (declared in
``pyproject.toml`` ``forecasting`` extra). When absent the trainer
exits with a clear error message; the predictor still works (it
falls back to floor-only predictions).
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claude_hpc.forecast.drain_simulator import simulate_drain
from claude_hpc.forecast.squeue_priority_field import parse_squeue_priority_field
from claude_hpc.forecast.wait_features import extract_features


def _read_snapshot(path: Path) -> str:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return f.read()
    return path.read_text(encoding="utf-8")


def _snapshot_time(path: Path) -> datetime | None:
    try:
        stem = path.name.split(".", 1)[0]  # strip .tsv.gz
        return datetime.strptime(stem, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _nearest_snapshot_before(
    snapshots: list[tuple[datetime, Path]],
    target: datetime,
    *,
    max_age_minutes: int = 30,
) -> Path | None:
    """Return the closest snapshot at or before *target*. ``None`` if
    no snapshot lies within ``max_age_minutes`` of the target — too
    stale to use as a feature input."""
    sorted_snaps = sorted(snapshots, key=lambda s: s[0])
    candidate: Path | None = None
    candidate_dt: datetime | None = None
    for dt, path in sorted_snaps:
        if dt > target:
            break
        candidate, candidate_dt = path, dt
    if candidate is None or candidate_dt is None:
        return None
    if (target - candidate_dt).total_seconds() > max_age_minutes * 60:
        return None
    return candidate


def build_training_rows(
    *,
    experiment_dir: Path,
    completed_jobs: list[dict[str, Any]],
    partition_slot_count_by_partition: dict[str, int],
    pending_walltime_default_sec: int,
) -> list[tuple[dict[str, Any], int]]:
    """Walk completed_jobs + saved snapshots → ``(features, overhead)``.

    *completed_jobs* is a list of dicts (typically from
    ``sacct -P --format=JobID,Submit,Start,Priority,Partition,User``)
    where each row carries:

    * ``submit_iso`` / ``start_iso`` — job's submit + actual start.
    * ``priority`` — the job's priority at submit.
    * ``partition`` / ``user`` — for feature extraction.
    * ``walltime_sec`` — what the user requested.

    Skips jobs missing a usable snapshot or with negative overhead.
    """
    snap_dir = experiment_dir / ".hpc" / "squeue_snapshots"
    if not snap_dir.is_dir():
        return []
    snapshots = [(t, p) for p in snap_dir.iterdir() if (t := _snapshot_time(p)) is not None]

    rows: list[tuple[dict[str, Any], int]] = []
    for job in completed_jobs:
        submit_dt = datetime.fromisoformat(job["submit_iso"])
        start_dt = datetime.fromisoformat(job["start_iso"])
        snap_path = _nearest_snapshot_before(snapshots, submit_dt)
        if snap_path is None:
            continue
        text = _read_snapshot(snap_path)
        queue = parse_squeue_priority_field(text)
        partition = job["partition"]
        slot_count = partition_slot_count_by_partition.get(partition, 1)
        floor_pess = simulate_drain(
            now_iso=submit_dt.isoformat(timespec="seconds"),
            queue=queue,
            partition=partition,
            partition_slot_count=slot_count,
            hypothetical_priority=int(job["priority"]),
            hypothetical_walltime_sec=int(job["walltime_sec"]),
            pending_walltime_default_sec=pending_walltime_default_sec,
            enable_backfill=False,
        )
        floor_opt = simulate_drain(
            now_iso=submit_dt.isoformat(timespec="seconds"),
            queue=queue,
            partition=partition,
            partition_slot_count=slot_count,
            hypothetical_priority=int(job["priority"]),
            hypothetical_walltime_sec=int(job["walltime_sec"]),
            pending_walltime_default_sec=pending_walltime_default_sec,
            enable_backfill=True,
        )
        floor_pess_iso = floor_pess.hypothetical_starts_at_iso or submit_dt.isoformat()
        floor_opt_iso = floor_opt.hypothetical_starts_at_iso or floor_pess_iso
        floor_pess_dt = datetime.fromisoformat(floor_pess_iso)
        floor_opt_dt = datetime.fromisoformat(floor_opt_iso)
        floor_pess_sec = int((floor_pess_dt - submit_dt).total_seconds())
        floor_opt_sec = int((floor_opt_dt - submit_dt).total_seconds())
        overhead_sec = int((start_dt - floor_pess_dt).total_seconds())
        if overhead_sec < 0:
            continue  # actual start before pessimistic floor — bad data, skip
        features = extract_features(
            now=submit_dt,
            queue=queue,
            your_priority=int(job["priority"]),
            your_partition=partition,
            your_constraint=str(job.get("constraint", "")),
            fairshare_by_user=job.get("fairshare_by_user"),
            recent_arrival_rate_per_hour=job.get("recent_arrival_rate_per_hour"),
            pessimistic_floor_sec=floor_pess_sec,
            optimistic_floor_sec=floor_opt_sec,
        )
        rows.append((features, overhead_sec))
    return rows


def fit_and_persist(
    *,
    rows: list[tuple[dict[str, Any], int]],
    experiment_dir: Path,
    val_fraction: float = 0.2,
) -> dict[str, Any]:
    """Fit LightGBM + write model + report. Returns a summary dict."""
    try:
        import lightgbm as lgb  # noqa: PLC0415
    except ImportError:
        print(
            "lightgbm is required for training; install via `pip install lightgbm`.",
            file=sys.stderr,
        )
        raise

    if not rows:
        return {"status": "no_training_data", "n_rows": 0}

    feature_names = sorted(rows[0][0].keys())
    X = [[_to_numeric(r[0].get(name, -1)) for name in feature_names] for r in rows]
    y = [r[1] for r in rows]
    n = len(rows)
    n_val = max(1, int(n * val_fraction))
    X_train, X_val = X[:-n_val], X[-n_val:]
    y_train, y_val = y[:-n_val], y[-n_val:]

    train_set = lgb.Dataset(X_train, label=y_train, feature_name=feature_names)
    val_set = lgb.Dataset(X_val, label=y_val, feature_name=feature_names, reference=train_set)
    booster = lgb.train(
        params={
            "objective": "regression",
            "metric": "mae",
            "learning_rate": 0.05,
            "num_leaves": 31,
            "verbose": -1,
        },
        train_set=train_set,
        valid_sets=[val_set],
        num_boost_round=500,
        callbacks=[lgb.early_stopping(stopping_rounds=20, verbose=False)],
    )

    out_dir = experiment_dir / ".hpc" / "wait_predictor"
    out_dir.mkdir(parents=True, exist_ok=True)
    model_path = out_dir / "model.txt"
    booster.save_model(str(model_path))

    val_pred = booster.predict(X_val)
    mae = sum(abs(p - t) for p, t in zip(val_pred, y_val, strict=True)) / len(y_val)
    pess_floor_idx = feature_names.index("pessimistic_floor_sec")
    opt_floor_idx = feature_names.index("optimistic_floor_sec")

    def _within_bracket(x: list[float], p: float) -> bool:
        lo = x[opt_floor_idx]
        hi = max(x[pess_floor_idx] * 5, x[pess_floor_idx] + 86400)
        return lo <= x[pess_floor_idx] + p <= hi

    bracket_pct = sum(
        1 for x, p in zip(X_val, val_pred, strict=True) if _within_bracket(x, p)
    ) / len(y_val)

    # Feature importance (gain) — tells you which features actually
    # moved the model. Surface in the summary so you can prune the
    # feature list when one or two clearly dominate.
    importances = list(booster.feature_importance(importance_type="gain"))
    feature_importance = sorted(zip(feature_names, importances, strict=True), key=lambda t: -t[1])

    summary = {
        "status": "ok",
        "n_train": len(X_train),
        "n_val": len(X_val),
        "val_mae_sec": mae,
        "bracket_pct": bracket_pct,
        "feature_names": feature_names,
        "feature_importance_gain": [
            {"feature": name, "gain": float(score)} for name, score in feature_importance
        ],
        "model_path": str(model_path),
        "trained_at": datetime.now(timezone.utc).isoformat(),
    }
    (out_dir / "training_summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def _to_numeric(value: Any) -> float:
    if value is None:
        return -1.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return -1.0


def _main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--experiment-dir", type=Path, default=Path("."))
    p.add_argument(
        "--completed-jobs",
        type=Path,
        required=True,
        help="JSON array of completed job dicts (see build_training_rows docstring).",
    )
    p.add_argument(
        "--slot-counts",
        type=Path,
        required=True,
        help="JSON dict mapping partition name → slot count.",
    )
    p.add_argument(
        "--pending-walltime-default-sec",
        type=int,
        default=86400,
        help="Default walltime for pending jobs without TimeLimit (24h).",
    )
    args = p.parse_args(argv)

    jobs = json.loads(args.completed_jobs.read_text())
    slot_counts = json.loads(args.slot_counts.read_text())
    rows = build_training_rows(
        experiment_dir=args.experiment_dir,
        completed_jobs=jobs,
        partition_slot_count_by_partition=slot_counts,
        pending_walltime_default_sec=args.pending_walltime_default_sec,
    )
    summary = fit_and_persist(rows=rows, experiment_dir=args.experiment_dir)
    print(json.dumps(summary, indent=2))
    return 0 if summary.get("status") == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(_main())
