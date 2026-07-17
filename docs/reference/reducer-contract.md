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

1. **Reads `$HPC_RUN_ID`** to find its inputs. The reducer typically uses `run_id` to discover the per-task `result_dir`s (via the run sidecar's `result_dir_template`) or to filter by run identity. On a pure-API backend the inputs are local, under `$HPC_RESULTS_DIR` — see [Where the reducer runs](#where-the-reducer-runs-cluster-vs-local).
2. **Reads `$HPC_AGGREGATED_OUTPUT`** to know where to write its single output. Defaults to `_aggregated/<run_id>.json` under `remote_path`. The reducer MUST write exactly this file; `cluster-reduce`'s `rsync_pull` includes only this basename.
3. **Writes valid JSON** to `$HPC_AGGREGATED_OUTPUT`. Any JSON shape — dict, list, scalar — is accepted. The cluster-reduce envelope's `data.reduced` parses and surfaces it inline.
4. **Exits 0 on success, non-zero on failure.** Stderr is captured (`stderr_tail` in the envelope, last ~2KB) and surfaced verbatim to the user when `exit_code != 0`.

That's it. No package, no plugin, no framework imports — the reducer can be a bash one-liner that pipes find + jq, or a 200-line numpy script.

## Where the reducer runs (cluster vs. local)

The contract is transport-neutral; only *where* the command runs depends on the backend's `requires_ssh` capability:

- **SSH backends** (SGE/SLURM/PBS): the reducer runs **on the cluster** over SSH (`cluster-reduce`), cwd = `remote_path`. Inputs are the per-task `result_dir`s on the shared filesystem; output is pulled back with a single `rsync_pull`.
- **Pure-API backends** (`requires_ssh = False`, e.g. GitHub Actions): there is no login node, so `aggregate-flow` first calls the backend's `fetch_results` to download the run's artifacts locally, then runs the **same reducer command on the control plane** (`local-reduce`), cwd = the fetched dir. Inputs live under **`$HPC_RESULTS_DIR`** (the fetched dir, layout `task-<i>/...`) rather than `remote_path`. Because it runs locally, the reducer's dependencies (numpy/pandas/…) must be importable **on the machine running `aggregate-flow`** — the cluster's run env is not available.

A portable reducer reads `$HPC_RESULTS_DIR` when set and falls back to its cluster convention otherwise, so the same script works on both paths.

## Where the interpreter comes from

`aggregate_cmd` is a shell command line, so a literal `python3` (or `python`) at the front of it has to resolve to *some* interpreter. On the cluster path, `cluster-reduce` prepends the **run's own environment activation** before running the command — the same activation the run's tasks used, derived from the run sidecar (`remote_activation_for_sidecar`). If the sidecar carries no env, it degrades to bare login `python`, the historical behaviour. So `python3 specs/reduce_x.py` binds the run's env interpreter, not whatever `python3` the login node happens to expose.

Two consequences for the author:

- Your reducer's imports (numpy/pandas/…) resolve against the **run's** environment — the exact environment the tasks ran under — so you don't install anything extra on the login node.
- A missing import is a miss in the run's pinned environment (surfaced as the interpreter/activation that was used), not a mystery cluster bug. Fix it where the run env is defined.

This retired a class where a run's tasks executed under one Python (say 3.13) but the reducer ran under the login node's default (say 3.8) and crashed on a version mismatch — the reducer now shares the run's interpreter.

On the local / pure-API path (`local-reduce`) there is no cluster env to activate, so the reducer runs under the control plane's interpreter and its deps must be importable *there* — as already noted under [Where the reducer runs](#where-the-reducer-runs-cluster-vs-local).

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

## Deployment and the on-disk requirement

How your reducer reaches the cluster depends on whether `aggregate_cmd` names a **file** or a **module**:

- **File-path reducer** — `python3 specs/reduce_x.py`. At submit, the framework derives the repo-relative reducer path from `aggregate_cmd` (`reducer_relpath_from_aggregate_cmd`, in the transport deploy-items layer), content-hashes the file, and **ships it with the run's staging** — alongside `.hpc/_hpc_combiner.py`. You never scp it by hand.
- **Module reducer** — `python -m pkg.reduce_x`. This names an *installed* module, not a repo file, so nothing extra is shipped: the run's environment push already carried whatever package provides it. If the module isn't importable in the run env, that's an env-definition problem, not a deployment one.

Because a file-path reducer is shipped *from disk*, it **must exist under the experiment repo at submit time**. If `aggregate_cmd` declares `specs/reduce_x.py` but that file is absent, submit **refuses at stage** (the reducer stage-gate in `submit_flow`) rather than letting the run compute for hours and then die mid-harvest with "no such file." Commit or create the reducer at the declared path before you submit — or switch to a `python -m` module reducer if it is genuinely installed in the run env.

## Streaming does not run your reducer yet (current limitation)

`aggregate-stream` — the progressive partial-aggregate that reports arms as they finish — reduces through the **built-in** weighted-mean (`reduce_metrics`) only. It does **not** invoke a run's custom `aggregate_cmd`. So for a run with a custom reducer, the streamed partial table can carry numbers that **differ from what your reducer would compute** (a QLIKE, a Diebold–Mariano statistic, a median — none of these is a plain mean of per-task scalars). Treat streaming output for a custom-reducer run as a liveness/progress signal, **not** as your reducer's result; the authoritative numbers come from the final `cluster-reduce` / `aggregate-flow` harvest, which does run your reducer.

This is a known open gap (tracked in [`docs/plans/s4-gaps-2026-07-17.md`](../plans/s4-gaps-2026-07-17.md), item 5); the resolution — invoke the custom reducer per complete arm, or refuse to stream a custom-reducer run — is pending a ruling and not yet built. See [`docs/primitives/aggregate-stream.md`](../primitives/aggregate-stream.md).

## When NOT to use cluster-reduce

If your metric is a per-task scalar that means-reduces correctly across tasks/waves (e.g. accuracy, MSE, log-likelihood per row), `_combiner/`'s output already has it. Skip the reducer; let `aggregate-flow`'s default `combiner-only` mode pull the small partials and finish locally.

The split rule: scalar-mean → combiner; anything-else → cluster-reduce.
