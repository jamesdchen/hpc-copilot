"""Minimal HPC executor scaffold — pure contract.

Copied into an experiment repo by ``/build-executor``. The runnable portion
of this file demonstrates only the executor contract; nothing about the
domain (ML, simulation, ETL, ...) is baked in. The LLM (or the user) fills
in :func:`compute` and adds whatever CLI flags the experiment requires.

Contract (same as every other hpc_mapreduce executor):

* Exposes an ``argparse`` CLI with ``if __name__ == "__main__":``.
* ``--output-file`` is the only required flag; add experiment-specific
  flags as needed (see the commented patterns at the bottom of this file
  and ``commands/build-executor.md`` for examples).
* Writes its result as a single file to ``--output-file`` (CSV by default;
  swap for Parquet/JSON as needed).
* Calls :func:`hpc_mapreduce.map.metrics_io.write_metrics` with a dict of
  scalar summaries when ``$RESULT_DIR`` is set. Skipped silently outside
  the HPC dispatcher so the scaffold stays runnable standalone.
* Has no knowledge of the scheduler. All parallelism is expressed via
  grid params handed in by ``_hpc_dispatch.py``.

Smoke test::

    python executor_template.py --output-file /tmp/out.csv
"""

# ruff: noqa: E501  (LLM-facing scaffolds read better with long lines)

from __future__ import annotations

import argparse
import csv
import os
from typing import Any


def compute(args: argparse.Namespace) -> dict[str, Any]:
    """Run the experiment and return a dict of scalar results.

    TODO: replace with the experiment's real computation. The returned
    dict becomes one CSV row (keys → header) and is also passed to
    ``write_metrics`` so the combiner can aggregate per grid point.

    Include ``n_samples`` (or any integer-valued weight) if you want the
    combiner's weighted-mean reduction to weight this task's contribution
    by something other than 1.
    """
    _ = args
    return {"value": 0.0, "n_samples": 0}


def build_parser() -> argparse.ArgumentParser:
    """Return the executor's argparse parser.

    Only ``--output-file`` is required by the contract. Add experiment-
    specific flags here — see the "Common patterns" block at the bottom
    of this file for typical examples (data path, horizon, date window,
    seed, shard id, ...).
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--output-file", required=True, help="where to write results")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    result = compute(args)

    # Persist as a one-row CSV. Replace with pandas/pyarrow/json as needed;
    # the only contract is "single file at --output-file".
    os.makedirs(os.path.dirname(args.output_file) or ".", exist_ok=True)
    with open(args.output_file, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(list(result.keys()))
        writer.writerow(list(result.values()))

    # Emit metrics.json sidecar so the cluster-side combiner can aggregate
    # per grid point. Skipped silently when running outside the HPC
    # dispatcher (no $RESULT_DIR) so the scaffold stays runnable standalone.
    if os.environ.get("RESULT_DIR"):
        from hpc_mapreduce.map.metrics_io import write_metrics

        write_metrics(result)

    print(f"[executor_template] wrote {args.output_file} result={result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# ── Common patterns (uncomment + edit as needed) ──────────────────────────
#
# The snippets below are reference material, not active code. Copy the
# pieces that match your experiment into the active scaffold above.
#
# 1. Standard grid-param flags. Add inside ``build_parser()``:
#
#    parser.add_argument("--data-path", default="data/")            # when: executor reads from a dataset root or URI
#    parser.add_argument("--horizon", type=int, default=1)          # when: forecasting / sequence task with a step-ahead target
#    parser.add_argument("--start", type=str)                       # when: a date/index lower bound varies across the grid
#    parser.add_argument("--end", type=str)                         # when: a date/index upper bound varies across the grid
#
# 2. ML training pattern (preserve verbatim from a typical forecasting
#    executor; uncomment and adapt). These are illustrative — nothing in
#    the framework requires this shape.
#
#    # PERIODS_PER_DAY = 48  # example: intraday forecasting
#    # def load_data(data_path): ...  # e.g. from lib.loading import load_raw_data
#    # def build_features(rows, horizon): ...  # HAR lags, calendar features, rolling stats
#    # def fit_and_predict(train_rows, test_rows): ...  # Ridge, XGBRegressor, torch.nn.Module
#    # def compute_metric(y_true, y_pred): ...  # QLIKE, MSE, MAE, accuracy, AUC
#    # y_true = [float(r.get("y", 0.0)) for r in test_rows]
#
# 3. See commands/build-executor.md "Common executor patterns" for fuller
#    ML / simulation / data-processing snippets.
