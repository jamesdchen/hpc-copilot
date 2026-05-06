# Reducer contract

The contract a user-side reducer follows so it can plug into `cluster-reduce` (and through it, `aggregate-flow` / `/aggregate-hpc` / `/campaign-hpc`'s iter-scoring step).

## Why a contract

The framework's combiner already does per-wave per-grid-point mean reduction over per-task `metrics.json` files; that's `_combiner/wave_<N>.json`. When the user's reducer is a simple mean over scalar metrics, no contract is needed — `aggregate-flow` reads the partials and reduces locally.

The contract exists for cases where per-task means aren't enough:
- Cross-task aggregation (concatenated forecast vs actuals → one QLIKE).
- Non-mean reducers (median, quantile, weighted by external task properties).
- Per-task outputs that aren't dicts (CSVs, parquet, pickles) — the combiner only reads `metrics.json`.

In those cases `aggregate-flow`'s default path either silently undercounts (combiner ignores raw chunks) or drags every per-task file to local (the `pull_summaries=True` failure mode). The cluster-reduce path gives the user a clean alternative: write a small reducer that runs on the cluster and emits one JSON file.

## The contract

A reducer is any executable (Python module, shell script, compiled binary) that:

1. **Reads `$HPC_RUN_ID`** to find its inputs. The reducer typically uses `run_id` to discover the per-task `result_dir`s (via the run sidecar's `result_dir_template`) or to filter by run identity.
2. **Reads `$HPC_AGGREGATED_OUTPUT`** to know where to write its single output. Defaults to `_aggregated/<run_id>.json` under `remote_path`. The reducer MUST write exactly this file; `cluster-reduce`'s `rsync_pull` includes only this basename.
3. **Writes valid JSON** to `$HPC_AGGREGATED_OUTPUT`. Any JSON shape — dict, list, scalar — is accepted. The cluster-reduce envelope's `data.reduced` parses and surfaces it inline.
4. **Exits 0 on success, non-zero on failure.** Stderr is captured (`stderr_tail` in the envelope, last ~2KB) and surfaced verbatim to the user when `exit_code != 0`.

That's it. No package, no plugin, no framework imports — the reducer can be a bash one-liner that pipes find + jq, or a 200-line numpy script.

## Minimal Python example

```python
# my_repo/scripts/qlike_reducer.py
"""Compute QLIKE across all per-task forecasts for the given run."""

from __future__ import annotations
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd


def main() -> int:
    run_id = os.environ["HPC_RUN_ID"]
    output = Path(os.environ["HPC_AGGREGATED_OUTPUT"])
    output.parent.mkdir(parents=True, exist_ok=True)

    # Find all per-task chunks for this run. Use whatever convention
    # your executor uses; the framework doesn't impose one here.
    chunks = list(Path("results").glob(f"{run_id}/task_*/forecast.csv"))
    if not chunks:
        print(f"no chunks found under results/{run_id}/", file=sys.stderr)
        return 1

    forecast = pd.concat([pd.read_csv(p) for p in chunks])
    qlike = float(np.mean(forecast["actual"] / forecast["pred"]
                          - np.log(forecast["actual"] / forecast["pred"]) - 1))
    output.write_text(json.dumps({
        "qlike": qlike,
        "n_chunks": len(chunks),
        "n_obs": len(forecast),
        "run_id": run_id,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

## Wiring it into a profile

Once the reducer exists, point `aggregate_defaults.aggregate_cmd` at it on the run sidecar (built via `build-submit-spec`'s `extra_env` or written directly via `write_run_sidecar`):

```python
write_run_sidecar(
    experiment_dir,
    run_id=run_id,
    ...,
    aggregate_defaults={
        "aggregate_cmd": "python -m scripts.qlike_reducer",
    },
)
```

Now `/aggregate-hpc` (and `/campaign-hpc`'s iter-score step) auto-routes through `cluster-reduce`. No bulk pull. The 1200-chunk failure mode is structurally eliminated.

## When NOT to use cluster-reduce

If your metric is a per-task scalar that means-reduces correctly across tasks/waves (e.g. accuracy, MSE, log-likelihood per row), `_combiner/`'s output already has it. Skip the reducer; let `aggregate-flow`'s default `combiner-only` mode pull the small partials and finish locally.

The split rule: scalar-mean → combiner; anything-else → cluster-reduce.
