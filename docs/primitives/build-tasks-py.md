---
name: build-tasks-py
verb: scaffold
side_effects:
- writes-sidecar: <experiment>/.hpc/tasks.py
idempotent: true
idempotency_key: experiment_dir
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent build-tasks-py --spec <path>
  python: claude_hpc.atoms.build_tasks_py.build_tasks_py
exit_codes:
- 0: ok
- 1: user-error
---

## Purpose

Scaffold the per-experiment `.hpc/tasks.py` from a cartesian-product axes spec + per-executor flag declarations. Replaces the slash-command prose that walked the agent through writing the file by hand. Defaults to **Pattern 1** (cartesian product) from `templates/tasks_example.py` — the 80% case for grid sweeps. Pattern 2 (chunking by row count) and Pattern 3 (date-window backtests) are hand-edits the user makes after generation; the primitive's `force=False` default preserves those edits.

## Compose with

- **Predecessors**: `discover-executors` (resolves which module paths go into `flags_by_executor`), `interview` (the user's stated axes ranges).
- **Successors**: `axes-init` (so the cold-start picker has a homogeneity hint), `compute-cmd-sha` (hashes the materialized task list), `build-submit-spec` → `submit-flow`.

## Notes

- **Output is syntactically valid Python.** Tests load the rendered file as a module and call `total()` / `resolve(i)`; rendering is asserted round-trippable.
- **Single vs multi-axis paths.** One axis renders as a plain list comprehension; two-or-more uses `itertools.product`, which means "leftmost varies slowest" (numpy / row-major), matching `compute_wave_map`'s convention.
- **Type tokens.** `flag()`'s second argument is a Python type. The primitive accepts class objects (`int`, `float`, `str`, `bool`) or string tokens (`"int"`, ...). Anything else routes to `str` since cluster-side argparse downcasts via the type ctor.
- **Default `force=False`.** Refuses to overwrite an existing `.hpc/tasks.py`. The user may have hand-edited a Pattern 2/3 conversion; we don't clobber it without explicit consent. Pass `force=True` to regenerate.
- **`flags_by_executor` is multi-key by design.** A repo with multiple executors (`src.ml_ridge`, `src.dl_patchts`, ...) gets one entry per executor. `/submit-hpc` picks one at submit time; the dispatcher errors fast on a missing entry, so include every executor that's a candidate.
