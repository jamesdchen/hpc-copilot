# Repo Audit — /loop sweeps to convergence

Every tracked file (~370) was audited. Sweeps ran parallel Opus agents;
fixes were applied with parallelized tool calls (no agent swarm for fixing).

## Sweep log

| Sweep | Scope | Agents | Real bugs fixed |
|-------|-------|--------|-----------------|
| 1 | Per-module audit (every `.py`, schemas, scripts, CI) | 11 | 15 + 1 (encoding bug surfaced by tests) |
| 2 | Cross-file / integration audit | 5 | 5 |
| 3 | Fresh-eyes re-audit + diff review | 5 | 8 |

Trend: 16 → 5 → 8, with severity falling each sweep. Sweep 3's diff-review
agent confirmed every prior fix is sound with zero regressions.

## Verification (after every sweep)

- `ruff check` + `ruff format` — clean on all changed files.
- `mypy` — no new errors (only pre-existing Windows-only `fcntl` noise in io.py).
- Full `pytest` suite diffed against the pre-audit baseline: **zero regressions**.
  Baseline had 41 failures (all pre-existing Windows-env: `os.setpgrp`,
  missing `rich`, `flock`, symlinks, ssh-gate — they pass on Linux CI);
  9 of them are now fixed, 32 remain (all pre-existing, out of audit scope).

## Bugs fixed (29)

### Encoding / Windows correctness
- `_internal/io.py`, `state/run_record.py`, `state/runs.py` (×3 readers),
  `campaign/manifest.py`, `ops/memory/interview.py` (×3 writers) — `read_text`/`write_text`
  without `encoding="utf-8"` → cp1252 round-trips / uncaught `UnicodeDecodeError`;
  `interview.py` made generator-mode `tasks.py` unimportable on Windows.
- `scripts/lint_primitive_modules.py` — substring path filter broke on Windows `\` separators.

### Logic / control flow
- `infra/backends/query.py` — qacct parse error misclassified as `NODE_FAIL`.
- `infra/inspect/sge.py` — wrong `slots` column index for pending SGE jobs.
- `infra/backends/slurm.py` + `sge.py` — `stderr_log_path` pointed at `_hpc_logs/`
  (never created); real logs land in `logs/`, plus a 1-based array-index off-by-one.
- `execution/mapreduce/combiner.py` + `reduce/metrics.py` — `_weighted_mean` crashed on a
  non-numeric `n_samples` weight (now coerced).
- `execution/mapreduce/reduce/history.py` — `result_dir_template` format specs (`{task_id:03d}`)
  silently broke campaign-history globbing.
- `flows/aggregate_flow.py` — `json.JSONDecodeError` not caught despite docstring promise.
- `runner/logs.py` — SSH transport failure masqueraded as a missing log.
- `runner/update_constraints.py` — unquoted `|` in `Features=` (shell metacharacter).
- `atoms/canary_verify.py` — unknown cluster silently defaulted scheduler to `slurm`.
- `ops/preflight/check.py` — missing `host` probed loopback instead of failing.
- `planning/throughput.py` — unparseable `max_walltime` → false "exceeds 0s" error.
- `forecast/age_priority_climb.py` — two-point near-zero Δt manufactured huge slope.
- `forecast/state_forecast.py` — `walltime_ask_sec` was read only off the
  co-tenant row, a key production snapshots don't populate, so the resource
  forecast was a silent no-op. Now falls back to the owning user's profile
  median (`median_walltime_ask_sec`) — the source `queue_simulator_inputs`
  already uses; an unprofiled user still degrades to "treated as running".
- `cli/_helpers.py` — pydantic v2 `ValidationError` (not a `ValueError`) → bad `--spec`
  mislabelled internal/exit-3 instead of user-error/exit-1; `submit-flow --dry-run`
  `KeyError` on a missing field.
- `infra/remote.py` — `_tar_ssh_push` leaked the tar stdout FD + zombie on timeout.
- `execution/mapreduce/templates/scaffolds/cli_dispatcher.py` — missing `spec is None` guard.

### Packaging / config
- plugin `pyproject.toml` — `hpc-agent>=0.3,<0.4` excluded the host's 0.4.0.
- `.pre-commit-config.yaml` — frontmatter hook missed `@primitive` outside `atoms/`.

### Docs / contracts corrected
- `runner/reconcile.py`, `errors.py`, `runner/logs.py` docstrings/text aligned to real paths.

## Deliberately NOT auto-fixed (need a maintainer decision)

- `atoms/campaign_converged.py` plateau check compares the recent window against
  the *all-time* prior best. Flagged by two sweeps, but "no new record in N iters"
  is a defensible plateau definition — left as a design call, not a clear bug.
- `execution/mapreduce/dispatch.py` WIP promotion uses flat `os.replace`; an executor that
  writes nested result subdirs would fail to promote on retry. A correct fix needs
  recursive merge — deferred rather than risk a wrong change.
- `runner/logs.py` `ssh_error` is now recorded on entries but `failures.py`
  `cluster_failures` still buckets them as `log_missing` — surfacing a distinct
  `ssh_unreachable` bucket is a product decision.

# Organization sweep — structural / drift pass

A separate workflow from the bug sweeps above: instead of hunting logic
bugs, this one targeted the repo's *organizational* health — the thing
CLAUDE.md/engineering-principles warns rots silently: prose facts drifting
from code, indexes losing entries, source-of-truth chains and enforcement
gates falling out of sync. A mechanical pre-scan (every generator in
`--check` mode + every `scripts/lint_*.py`) ran first; four parallel Opus
agents then covered the judgment dimensions (doc↔code drift, index /
cross-reference integrity, gate-enforcement coverage, source-tree
placement). Every agent finding was re-verified against code before any
edit. Verification after: `ruff`, the full lint suite, all SoT `--check`
gates, and `tests/contracts/` + `tests/contract/` (480 passed, 73
pre-existing xfails) — zero regressions.

## Enforcement gaps closed

- `lint_decision_content.py` existed but was wired into **no** gate
  (pre-commit, CI, or test) — and was already **failing**: the
  `inline-isolation-ceiling` block had drifted, a submit-only paragraph
  captured inside the shared markers. Re-scoped the markers so the three
  workflow SKILLs' shared block is byte-identical again, then wired the
  lint into pre-commit + CI (making `architecture.md`'s "sibling lint …
  enforces this" claim true).
- `lint_text_io_encoding.py` and `lint_schema_versions.py` ran in
  pre-commit only — added to CI so a contributor without pre-commit
  installed can't bypass them.

## Drift fixed (doc ↔ code)

- `boundary-contract.md` — claimed a "15-name" surface (actually 16),
  listed a phantom `HPC_SUBDIR` export (the `.hpc` name is a layout
  literal, not exported) and the moved `ssh_run`/`rsync_*`/`deploy_runtime`
  names as current surface (they live in the `infra/` deprecation shim).
- `sync-checklist.md` — `error_code` "16 values" → 17 (added
  `model_endpoint_error`); `failure_category` list missing `segv`; a
  "Known discrepancies" section describing a double-source that had since
  been collapsed (`CATEGORIES = typing.get_args(FailureCategory)`), citing
  a nonexistent `ResubmitCategory` / `_wire/resubmit.py`; `EnvelopeAdapter`
  path; `compute_cmd_sha` relocated to `state.run_sha`.
- `architecture.md` — a `recover-flow` row under "workflow primitives"
  that isn't a registered primitive (`recover_flow.py` hosts a plain
  `resubmit_flow()`); `LifecycleState`/`FailureCategory` placed in a
  nonexistent `lifecycle/lifecycle.py` (they're `StrEnum`s in
  `contract/vocabulary.py`); stale campaign-atoms inventory.
- Five stale test-path citations (`tests/test_boundary_contract.py`,
  `tests/test_agent_facing_partition.py`, `tests/test_resubmit_batching.py`)
  now point at their real `tests/contracts/` / `tests/ops/recover/` homes.
- `state/__init__.py` + `ops/recover/README.md` + `remote_factory.py`
  docstrings/prose — `compute_cmd_sha` location and the `resubmit-flow`
  → `resubmit-failed` primitive name.
- `docs/internals/README.md` index was missing `experiment-contract.md`
  and `mutation-testing.md` (the latter orphaned — referenced nowhere).

## Deliberately NOT auto-fixed (need a maintainer decision)

- `ops/transfer/` is an inert one-module subject (`manifest.py`, no
  `@primitive`, no `src` importer) staged for issue #232. Per
  `architecture.md`'s own rule a profile-independent transport helper
  belongs in `infra/`, not an empty subject — but it's deliberate staging,
  so land-the-workflow vs relocate-to-`infra/` is a maintainer call. Left
  out of `architecture.md`'s canonical subject list until that resolves.
- Several lints (`lint_subject_init`, `lint_plugin_manifests`,
  `lint_skill_command_sync`) run in a gate but have no *fire-path* test
  exercising a synthetic violation — engineering-principles asks for one
  per rule. Adding them is test authoring, not drift repair; recorded for
  a follow-up.
- `architecture.md` per-subject atom enumerations drift as atoms are added
  (the boxes read as exhaustive). The principled fix is to make them
  representative rather than chase every atom — an editorial pass left for
  a maintainer.
