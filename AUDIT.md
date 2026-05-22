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
- `_internal/io.py`, `_internal/session/run_record.py`, `state/runs.py` (×3 readers),
  `campaign/manifest.py`, `atoms/interview.py` (×3 writers) — `read_text`/`write_text`
  without `encoding="utf-8"` → cp1252 round-trips / uncaught `UnicodeDecodeError`;
  `interview.py` made generator-mode `tasks.py` unimportable on Windows.
- `scripts/lint_primitive_modules.py` — substring path filter broke on Windows `\` separators.

### Logic / control flow
- `infra/backends/query.py` — qacct parse error misclassified as `NODE_FAIL`.
- `infra/inspect/sge.py` — wrong `slots` column index for pending SGE jobs.
- `infra/backends/slurm.py` + `sge.py` — `stderr_log_path` pointed at `_hpc_logs/`
  (never created); real logs land in `logs/`, plus a 1-based array-index off-by-one.
- `mapreduce/combiner.py` + `reduce/metrics.py` — `_weighted_mean` crashed on a
  non-numeric `n_samples` weight (now coerced).
- `mapreduce/reduce/history.py` — `result_dir_template` format specs (`{task_id:03d}`)
  silently broke campaign-history globbing.
- `flows/aggregate_flow.py` — `json.JSONDecodeError` not caught despite docstring promise.
- `runner/logs.py` — SSH transport failure masqueraded as a missing log.
- `runner/update_constraints.py` — unquoted `|` in `Features=` (shell metacharacter).
- `atoms/canary_verify.py` — unknown cluster silently defaulted scheduler to `slurm`.
- `atoms/preflight.py` — missing `host` probed loopback instead of failing.
- `planning/throughput.py` — unparseable `max_walltime` → false "exceeds 0s" error.
- `forecast/age_priority_climb.py` — two-point near-zero Δt manufactured huge slope.
- `forecast/state_forecast.py` — `walltime_ask_sec` was read only off the
  co-tenant row, a key production snapshots don't populate, so the resource
  forecast was a silent no-op. Now falls back to the owning user's profile
  median (`median_walltime_ask_sec`) — the source `queue_simulator_inputs`
  already uses; an unprofiled user still degrades to "treated as running".
- `agent_cli.py` — pydantic v2 `ValidationError` (not a `ValueError`) → bad `--spec`
  mislabelled internal/exit-3 instead of user-error/exit-1; `submit-flow --dry-run`
  `KeyError` on a missing field.
- `infra/remote.py` — `_tar_ssh_push` leaked the tar stdout FD + zombie on timeout.
- `mapreduce/templates/scaffolds/cli_dispatcher.py` — missing `spec is None` guard.

### Packaging / config
- `hpc-agent-pro/pyproject.toml` — `hpc-agent>=0.3,<0.4` excluded the host's 0.4.0.
- `.pre-commit-config.yaml` — frontmatter hook missed `@primitive` outside `atoms/`.

### Docs / contracts corrected
- `runner/reconcile.py`, `errors.py`, `runner/logs.py` docstrings/text aligned to real paths.

## Deliberately NOT auto-fixed (need a maintainer decision)

- `atoms/campaign_converged.py` plateau check compares the recent window against
  the *all-time* prior best. Flagged by two sweeps, but "no new record in N iters"
  is a defensible plateau definition — left as a design call, not a clear bug.
- `mapreduce/dispatch.py` WIP promotion uses flat `os.replace`; an executor that
  writes nested result subdirs would fail to promote on retry. A correct fix needs
  recursive merge — deferred rather than risk a wrong change.
- `runner/logs.py` `ssh_error` is now recorded on entries but `failures.py`
  `cluster_failures` still buckets them as `log_missing` — surfacing a distinct
  `ssh_unreachable` bucket is a product decision.
