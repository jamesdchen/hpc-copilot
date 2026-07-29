---
name: discover
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-agent discover [--experiment-dir <dir>] [--kind <kind>] [--search-dirs
    <search_dirs>]
  python: hpc_agent.state.discover.discover
exit_codes:
- 0: ok
- 3: internal
---

## Purpose

Scan `experiment_dir` for one kind of artifact — the single discovery verb
(merged from the former `discover-executors` / `discover-runs` /
`discover-reducers` registrations). `--kind` selects the scan; the result
carries `kind` plus exactly the matching key, and each kind keeps the result
key its pre-merge verb emitted, so `data.executors` / `data.runs` /
`data.reducers` consumers are unaffected.

- `--kind executors` (default) — every Python file that looks like an
  executor: the **new contract** exports `compute(args) -> None` (CLI dispatch
  lives in the auto-generated `.hpc/cli.py`), the **old (transitional)
  contract** self-dispatches via an `if __name__ == "__main__":` guard plus a
  recognized CLI framework (`argparse | click | typer | fire`). Drives the
  executor-selection step of `/submit-hpc`.
- `--kind runs` — every `@register_run`-decorated function (path, name, `gpu`,
  `run_signature_sha`, `flags`). Gives the headless worker a CLI verb for run
  discovery so it never shells `python .hpc/scaffold.py discover` (arbitrary
  Python). AST-walks `.py` and `.ipynb` recursively; result cached by a tree
  fingerprint (#264).
- `--kind reducers` — candidate reducer / aggregator scripts, matched
  generously by filename stem (`aggregate`, `qlike`, `rmse`, …) or a top-level
  `aggregate` / `reduce` / `score` / `evaluate` / `summarize` function. Used at
  `/aggregate-hpc` time so the agent finds the reducer the user already
  committed instead of writing a fresh one.

## Inputs

- `--kind` (string, `executors` | `runs` | `reducers`, default `executors`) —
  which scan to run.
- `--search-dirs` (comma-separated names, optional) — subdirectories to scan,
  for `--kind executors` / `reducers` only (default `executors,scripts,src`
  with a fallback to the experiment-dir root). Refused loudly with
  `--kind runs`: the runs scan walks the whole tree by contract, and silently
  ignoring the flag would misreport what was scanned.

## Outputs

`{kind, executors | runs | reducers}` — `kind` echoes the selected scan; the
matching key holds the sorted entry list. Entry shapes are per kind (see the
schema): executor entries carry the contract-classification fields
(`has_main_guard`, `cli_framework`), run entries carry `run_signature_sha` (the
axis-classification cache key) and `flags`, reducer entries carry the matched
hint signals and first docstring line.

## Errors

`spec_invalid` on an unknown `--kind` or on `--search-dirs` with
`--kind runs`. A repo where the scan finds nothing is a valid empty result,
never an error — for executors the `/submit-hpc` flow scaffolds one (via
[build-executor](build-executor.md)) inline before continuing.

## Idempotency

Pure local filesystem walk (no SSH, no cluster contact); same tree, same
result. The runs scan is additionally cached by a tree fingerprint —
`HPC_NO_DISCOVER_CACHE=1` disables the cache.

## Compose with

- `--kind executors`: successors `score-submit-plan`, `submit-spec` — the
  chosen `executors[].name` typically becomes `spec.profile` downstream.
- `--kind runs`: the entry point of `/submit-hpc` Step 1 and the first
  sub-call of `classify-axis-preflight`; successors `classify-axis` (keyed by
  `run_signature_sha`), `build-tasks-py`, `build-submit-spec`.
- `--kind reducers`: composed by `/aggregate-hpc` at the "find a canonical
  reducer" step, so the agent asks "use `aggregators/qlike.py` or write a new
  one?" instead of defaulting to write-new.

## Notes

- **Run contract vs executor contract.** `--kind runs` finds `@register_run`
  functions (the framework's typed-kwarg contract); `--kind executors` finds
  CLI-style executor scripts. They scan for different things; a repo may have
  either or both.
- The underlying scan functions (`discover_executors`, `discover_runs`,
  `discover_reducers` in `state/discover.py`) remain plain Python callables
  for in-process composers (`interview`, `wrap-entry-point`,
  `export-package`); this verb owns only the dispatch and the envelope
  projection.
- Reducer detection is deliberately generous: the failure mode it prevents
  (the agent writes a fresh QLIKE / RMSE aggregator when the user already
  committed one) costs more than a false positive.

**Schemas:** [`discover.output.json`](../../src/hpc_agent/schemas/discover.output.json).
