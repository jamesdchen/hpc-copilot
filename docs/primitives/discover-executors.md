---
name: discover-executors
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-agent discover --experiment-dir <path> [--search-dirs <a,b,c>]
  python: hpc_agent.state.discover.discover_executors
exit_codes:
- 0: ok
- 3: internal
---

## Purpose

List every Python file under `experiment_dir` that looks like a CLI executor (has a `__main__` guard and a recognized argument-parsing framework). The output drives the executor-selection step of `submit-hpc`.

## Compose with

- Common predecessors: `check-preflight` (cheap; doesn't strictly require it).
- Common successors: `score-submit-plan`, `submit-spec`. The `executors[].name` chosen here typically becomes `spec.profile` downstream.

## Notes

- **Executor contract classification** (driven by `has_compute_function` + `has_main_guard`):
  - `has_compute_function == true` → **new contract**. The executor exports `compute(args) -> None`. CLI dispatch lives in the auto-generated `.hpc/cli.py`; the executor file itself is pure compute. Per-executor flag list lives in `.hpc/tasks.py` `FLAGS[<module>]`, not in the executor file.
  - `has_compute_function == false and has_main_guard == true` → **old (transitional) contract**. The executor self-dispatches via `if __name__ == "__main__":` plus a recognized CLI framework (`cli_framework` in `argparse | click | typer | fire`). Run `python3 <info.path> --help` to map the CLI interface.
  - Both false → not an executor (utility module, `__init__.py`, etc.); the primitive filters these out.
- The scanner walks `executors/`, `scripts/`, and `src/` by default and falls back to the experiment-dir root when none of those exist. Callers that know their directory convention (e.g. an integrator that treats `src/` as modules-only) should pass `--search-dirs scripts` on the CLI (or `search_dirs=("scripts",)` to the Python API); hpc-agent does not auto-detect layout markers.
- A repo with zero discovered executors is a valid result; the slash command's flow is to scaffold one (via [build-executor](build-executor.md)) inline before continuing.
- Pure local filesystem walk; no SSH, no cluster contact.
