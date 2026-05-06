---
name: discover-reducers
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-mapreduce discover-reducers --experiment-dir <path>
  python: claude_hpc.state.discover.discover_reducers
exit_codes:
- 0: ok
- 3: internal
---

## Purpose

List every Python file under `experiment_dir` that looks like a **reducer** / **aggregator** — the user-side counterpart to per-task executors. Detection is intentionally generous because the failure mode it exists to prevent (the agent writes a fresh QLIKE / RMSE / MAE aggregator when the user already committed one) is more costly than the occasional false positive.

A file qualifies on either signal:

1. **Filename stem** matches a reducer hint as a substring: `aggregate`, `aggregator`, `reduce`, `reducer`, `evaluate`, `evaluation`, `score`, `scoring`, `metric`, `metrics`, plus loss-function names `qlike`, `rmse`, `mae`, `mse`, `mape`, `smape`, `loss`, `summarise`, `summarize`. So `qlike.py`, `aggregate_qlike.py`, `compute_rmse.py` all match.
2. **Top-level function** named `aggregate`, `reduce`, `score`, `evaluate`, `summarize`, or `summarise` — with at least one positional parameter (zero-arg `def aggregate():` is rejected as too generic, mirroring the `compute(args)` heuristic).

Either alone qualifies; both is fine. Multi-signal matches sort first in the returned list so the agent's "best candidate" picks naturally surface what's most likely the canonical reducer.

## Compose with

- Common predecessors: none — this is a query primitive that runs in isolation.
- Common successors: the `/aggregate-hpc` slash command. Step 4 invokes this primitive when no `aggregate_defaults.aggregate_cmd` is recorded on the run sidecar; if a candidate is found the agent uses it as the cluster-side aggregation command instead of writing a new one.

## Notes

- **Search dirs**: by default `aggregators/`, `reducers/`, `scoring/`, `scripts/`, `src/`. Recursive walk (reducers often live nested at `src/eval/qlike.py`); contrast `discover-executors` which is non-recursive because executors live at predictable top-level paths.
- **Excluded dirs**: `.hpc/`, `.git/`, `__pycache__/`, `.mypy_cache/` — same as `discover-executors`.
- **Each `ReducerInfo` carries `path`, `name`, `matches`, `docstring`.** `matches` is the list of signals that hit (e.g. `["name:qlike", "function:aggregate"]`); the slash command surfaces this verbatim so the user can see *why* a candidate was suggested.
- **False positives are tolerated by design.** Better to surface an extra candidate the user dismisses than to silently miss the real reducer and let the agent write a duplicate.
