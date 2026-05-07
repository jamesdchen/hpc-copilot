---
name: discover-reducers
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-agent discover-reducers --experiment-dir <path>
  python: claude_hpc.state.discover.discover_reducers
exit_codes:
- 0: ok
- 3: internal
---
# discover-reducers

> **Internal primitive.** Composed by the `/aggregate-hpc` slash
> command at the "find a canonical reducer" step. Direct
> invocation is for debugging.

Walk `experiment_dir` for Python files that look like reducers /
aggregators — the user-side counterpart to per-task executors.
Detection is generous because the failure mode this prevents
(agent writes a fresh QLIKE / RMSE aggregator when the user
already committed one) is more costly than a false positive.

A file qualifies on either signal:

1. **Filename stem** matches a reducer hint substring:
   `aggregate`, `reducer`, `evaluate`, `score`, `metric`, plus
   loss names (`qlike`, `rmse`, `mae`, `mse`, `loss`,
   `summarize`, …).
2. **Top-level function** named `aggregate`, `reduce`, `score`,
   `evaluate`, `summarize`, `summarise` with at least one
   positional parameter.

Either alone qualifies. Multi-signal matches sort first.

## Composers

- `/aggregate-hpc` slash command, Step 4 (when no
  `aggregate_defaults.aggregate_cmd` is recorded on the run
  sidecar).

No registered Python `composes=` references.

## Invariants

- **Pure read.** No filesystem mutation, no SSH.
- **Generous matching is the contract.** False positives are
  tolerated by design — better to surface an extra candidate the
  user dismisses than to silently miss the real reducer.
- **Sort order is stable**: multi-signal matches first, then
  alphabetical. Slash commands key on the first entry as the
  default suggestion.

## Coupling

- The reducer-name vocabulary (substring list + top-level
  function names) is hardcoded in this atom. Adding a new
  vocabulary entry means editing this file plus updating the
  user-facing list in `docs/primitives/discover-reducers.md`
  itself.
- Search dirs (`aggregators/`, `reducers/`, `scoring/`,
  `scripts/`, `src/`) are configurable via `search_dirs=`; the
  default fallback to `root` if none of these exist is the
  contract. Renaming the defaults breaks every existing repo's
  cold-start path.

## Failure modes

- A reducer file under an excluded dir (`.hpc/`, `.git/`,
  `__pycache__/`, `.mypy_cache/`) is silently dropped. Cosmetic
  reason: those are framework or tooling territory.
- A function with the right name but zero positional params
  (`def aggregate():` with all kwargs) → rejected as too
  generic. Same heuristic as `discover-executors`'s
  `compute(args)` rule.
