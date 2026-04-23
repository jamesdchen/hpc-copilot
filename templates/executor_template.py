"""Minimal HPC executor scaffold.

Copied into an experiment repo by ``/build-executor``. Every ``# TODO:`` marker
is a point the LLM (or the user) is expected to fill in when customising for a
specific model, metric, or data source. The file as-written is runnable —
``python executor_template.py --help`` and ``python executor_template.py
--output-file /tmp/out.csv`` both exit cleanly — so the smoke test in
``/build-executor`` Step 4 can succeed on a freshly-scaffolded copy.

Contract (same as every other hpc_mapreduce executor):

* Exposes an ``argparse`` CLI with ``if __name__ == "__main__":``.
* Accepts the standard grid-param flags: ``--data-path``, ``--horizon``,
  ``--start``, ``--end``, ``--output-file``. Additional flags are free-form.
* Writes its result as a single file to ``--output-file`` (CSV by default;
  swap for Parquet/JSON as needed).
* Has no knowledge of the scheduler; all parallelism is expressed via grid
  params handed in by ``_hpc_dispatch.py``.
"""

# ruff: noqa: E501  (LLM-facing scaffolds read better with long lines)

from __future__ import annotations

import argparse
import csv
import os
from typing import Any

# ── Constants ─────────────────────────────────────────────────────────────
# TODO: adjust or delete constants that do not apply to your experiment.
PERIODS_PER_DAY = 48


# ── Data loading ──────────────────────────────────────────────────────────
def load_data(data_path: str) -> list[dict[str, Any]]:
    """Return the full input dataset as a list of row dicts.

    TODO: replace with your real loader — e.g.
        ``from lib.loading import load_raw_data``
        ``return load_raw_data(data_path)``

    The default stub returns an empty list so the scaffold runs without a
    real dataset.
    """
    _ = data_path
    return []


# ── Feature engineering ───────────────────────────────────────────────────
def build_features(rows: list[dict[str, Any]], horizon: int) -> list[dict[str, Any]]:
    """Apply feature engineering and horizon-shift the target.

    TODO: implement — HAR lags, calendar features, rolling stats, etc.
    """
    _ = horizon
    return rows


# ── Model ─────────────────────────────────────────────────────────────────
def fit_and_predict(
    train_rows: list[dict[str, Any]],
    test_rows: list[dict[str, Any]],
) -> list[float]:
    """Fit on train, predict on test. Return one prediction per test row.

    TODO: instantiate your model (Ridge, XGBRegressor, torch.nn.Module, ...),
    fit it, and return predictions aligned with ``test_rows``.
    """
    _ = train_rows
    return [0.0 for _ in test_rows]


# ── Metric ────────────────────────────────────────────────────────────────
def compute_metric(y_true: list[float], y_pred: list[float]) -> float:
    """Return a scalar summarising prediction quality.

    TODO: replace with your metric — QLIKE, MSE, MAE, accuracy, AUC, ...
    """
    if not y_true:
        return 0.0
    return sum((t - p) ** 2 for t, p in zip(y_true, y_pred, strict=True)) / len(y_true)


# ── CLI ───────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)

    # Standard grid-param flags. Keep these.
    parser.add_argument("--data-path", default="data/", help="dataset root or URI")
    parser.add_argument("--horizon", type=int, default=1, help="forecast horizon")
    parser.add_argument("--start", type=int, default=0, help="inclusive row-index lower bound")
    parser.add_argument("--end", type=int, default=-1, help="exclusive upper bound (-1 = end)")
    parser.add_argument("--output-file", required=True, help="where to write results")

    # TODO: add model-specific flags (e.g. --alpha, --n-estimators, --epochs).
    # parser.add_argument("--alpha", type=float, default=1.0)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # 1. Load.
    rows = load_data(args.data_path)

    # 2. Slice to this task's chunk.
    end = len(rows) if args.end == -1 else args.end
    rows = rows[args.start : end]

    # 3. Feature engineering + horizon shift.
    rows = build_features(rows, args.horizon)

    # 4. TODO: split rows into train/test (or otherwise slice) as your experiment requires.
    train_rows: list[dict[str, Any]] = rows[:-1] if len(rows) > 1 else []
    test_rows: list[dict[str, Any]] = rows[-1:] if rows else []

    # 5. Fit + predict.
    y_pred = fit_and_predict(train_rows, test_rows)

    # 6. TODO: extract your ground-truth column from test_rows.
    y_true = [float(r.get("y", 0.0)) for r in test_rows]
    metric = compute_metric(y_true, y_pred)

    # 7. Persist results. Replace with pandas/pyarrow if preferred.
    os.makedirs(os.path.dirname(args.output_file) or ".", exist_ok=True)
    with open(args.output_file, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["horizon", "n_rows", "metric"])
        writer.writerow([args.horizon, len(test_rows), metric])

    # 8. Emit metrics.json alongside raw outputs so the cluster-side combiner
    #    can aggregate per grid point. Skipped silently when running outside
    #    the HPC dispatcher (no $RESULT_DIR) so the scaffold stays runnable
    #    standalone for smoke tests.
    # TODO: extend the metrics dict with any other scalar summaries you want
    #    rolled up (mse, qlike, auc, ...). ``n_samples`` becomes the weight
    #    in the combiner's weighted mean.
    if os.environ.get("RESULT_DIR"):
        from hpc_mapreduce.map.metrics_io import write_metrics

        write_metrics({"metric": metric, "n_samples": len(test_rows)})

    print(f"[executor_template] wrote {args.output_file} metric={metric:.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
