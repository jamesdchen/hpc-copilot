---
name: build-tasks-py
verb: scaffold
side_effects:
- writes-sidecar: <experiment>/.hpc/tasks.py
- writes-sidecar: <experiment>/.hpc/cli.py
idempotent: true
idempotency_key: experiment_dir
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent build-tasks-py [--experiment-dir <dir>] --spec <spec> [--force]
  python: hpc_agent.incorporation.build.tasks_py.build_tasks_py
exit_codes:
- 0: ok
- 1: user-error
---

## Purpose

Scaffold the per-experiment `.hpc/tasks.py` from a cartesian-product axes spec + per-executor flag declarations. Replaces the slash-command prose that walked the agent through writing the file by hand. Defaults to **Pattern 1** (cartesian product) from `templates/tasks_example.py` — the 80% case for grid sweeps. Pattern 2 (chunking by row count) and Pattern 3 (date-window backtests) are hand-edits the user makes after generation; the primitive's `force=False` default preserves those edits.

**Planner mode.** When the spec carries a `data_axis`, the cartesian `axes` become the *sweep* and the series axis is partitioned by `hpc_agent.experiment_kit.plan_tasks`: the primitive runs the planner at scaffold time and bakes the resolved task list into a `_TASKS` literal. Each task then carries its sweep point plus `start` / `end` / `halo` slice keys. The generated file imports only `executor_cli` — same runtime footprint as a cartesian one — so it loads in the stdlib-only cluster dispatcher. The classification that fills `data_axis` is described below.

## Classifying the DataAxis

Planner mode needs one decision: how is the series axis safe to split? Parallelizing a computation is partitioning a totally-ordered series; the partition is *fungible* with a serial run only if it does not cut an unaccounted data dependency. Read `compute()` (or the experiment's `run()`) and its call graph and pick the case:

| Observation in the loop | `data_axis.kind` | `halo_expr` |
|---|---|---|
| Loop body is a pure function of its row (no accumulator) | `independent` | — |
| Accumulates an *associative* summary (sum, count, min/max, sufficient statistics) | `associative` (set `monoid`) | — |
| Refits / re-reads a *trailing window* of bounded length (rolling stat, `train_window` lookback) | `bounded_halo` | ≈ the window length, e.g. `params['train_window'] * 48` |
| Unbounded or order-dependent dependency (running state with no fixed horizon; trial *n* depends on `0..n-1`) | `sequential` | — |

Two rules — this is real program analysis and it is sometimes wrong:

- **Default to `sequential` on any uncertainty.** A serial run is slow, not wrong. Narrow to a splittable kind only when the code makes the dependency structure unambiguous.
- **Bias halos large.** An over-wide halo wastes compute; a too-small halo is silent corruption. Set `halo_expr` to the full window, never a guess below it.

The classification is **never trusted unverified**: gate it with `hpc_agent.experiment_kit.check_elision` / `assert_elision_equivalent` (run the experiment whole vs. split N ways, assert agreement) before submitting, and wire `assert_elision_equivalent` into the experiment repo's CI. A misclassified axis runs fine and returns plausible-but-wrong numbers — the elision gate is the only thing that catches it.

## Compose with

- **Predecessors**: `discover-executors` (resolves which module paths go into `flags_by_executor`), `interview` (the user's stated axes ranges).
- **Successors**: `axes-init` (so the cold-start picker has a homogeneity hint), `compute-cmd-sha` (hashes the materialized task list), `build-submit-spec` → `submit-flow`.

## Notes

- **Output is syntactically valid Python.** Tests load the rendered file as a module and call `total()` / `resolve(i)`; rendering is asserted round-trippable.
- **Single vs multi-axis paths.** One axis renders as a plain list comprehension; two-or-more uses `itertools.product`, which means "leftmost varies slowest" (numpy / row-major), matching `compute_wave_map`'s convention.
- **Type tokens.** `flag()`'s second argument is a Python type. The primitive accepts class objects (`int`, `float`, `str`, `bool`) or string tokens (`"int"`, ...). Anything else routes to `str` since cluster-side argparse downcasts via the type ctor.
- **Default `force=False`.** Refuses to overwrite an existing `.hpc/tasks.py`. The user may have hand-edited a Pattern 2/3 conversion; we don't clobber it without explicit consent. Pass `force=True` to regenerate.
- **`flags_by_executor` is multi-key by design.** A repo with multiple executors (`src.ml_ridge`, `src.dl_patchts`, ...) gets one entry per executor. `/submit-hpc` picks one at submit time; the dispatcher errors fast on a missing entry, so include every executor that's a candidate.
- **Planner mode bakes, never re-computes.** `plan_tasks` runs once at scaffold time and the resolved `_TASKS` list is written as a literal — the generated `tasks.py` carries no `hpc_agent.experiment_kit` import. `data_axis.series_length` is the integer the caller probed; `halo_expr` is validated to arithmetic over `params` (no calls/imports) before it is evaluated.
- **Reserved axis names.** The dispatcher exports each `resolve()` kwarg as both `HPC_KW_<KEY>` and a bare uppercase `<KEY>`; a bare name colliding with a real env var (an axis `home` → `$HOME`) corrupts the executor's environment. `build-tasks-py` rejects axis names whose uppercase form is in a reserved set — `HOME` / `PATH` / `USER` / `LD_LIBRARY_PATH` / `OMP_NUM_THREADS`, the framework's `HPC_*`, scheduler-injected `SLURM_*` / `SGE_*` / `PBS_*`, etc. — at scaffold time. Prefer experiment-prefixed names (`exp_horizon`, `ridge_alpha`); or set `HPC_KW_NAMESPACE_ONLY=1` in the spec's `job_env` to disable the bare-uppercase export entirely (the recommended default for new campaigns — the executor then reads `HPC_KW_<KEY>` only).
