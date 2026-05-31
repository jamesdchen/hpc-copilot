---
name: discover-runs
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-agent discover-runs [--experiment-dir <dir>]
  python: hpc_agent.state.discover.discover_runs
exit_codes:
- 0: ok
- 3: internal
---

## Purpose

List every `@register_run`-decorated function under `experiment_dir` — the run contract (a typed-kwarg Python function that may live in a notebook, script, or module). Returns each run's `path`, `name`, `gpu` flag, `run_signature_sha` (the cache key for axis classification), and `flags`. Gives the headless `submit-hpc` worker a CLI verb for run discovery so it never shells `python .hpc/scaffold.py discover` (arbitrary Python) to find the decorated function.

## Compose with

- Common predecessors: none — this is the entry point of `/submit-hpc` Step 1.
- Common successors: `classify-axis` (keyed by `run_signature_sha`), `build-tasks-py`, `build-submit-spec`. The chosen run's `name` typically becomes `spec.profile` downstream.

## Notes

- **Run contract vs executor contract.** `discover-runs` finds `@register_run` functions (the framework's typed-kwarg contract); [discover-executors](discover-executors.md) finds CLI-style executor scripts (`__main__` + a recognized argument-parsing framework). They scan for different things; a repo may have either or both.
- AST-walks `.py` and `.ipynb` files recursively, skipping `.hpc/` and VCS/cache directories. A file that fails to parse is skipped, not fatal.
- `run_signature_sha` is the stable key Step 3 uses to look up a recorded axis classification in `.hpc/axes.yaml`; it changes when the run's signature changes, invalidating a stale classification.
- Pure local filesystem walk; no SSH, no cluster contact.
