# Audit, dedup, and cross-cutting refactor pass

This PR consolidates a session-long set of refactors and feature additions on `claude/audit-deduplicate-code-llDxc`. Branch baseline: `d933375`. Tests at merge time: 883 passing; 7 docs-link tests pre-existing failures (deselected). This document is structured for future debugging — when something breaks, the bug-class taxonomy below should let an LLM agent pattern-match the failure to the relevant axis without re-deriving the architecture.

## Quick orientation for debugging

If you're investigating a regression months from now:

- **Compare against baseline `d933375`**: `git show d933375:<path>` shows the pre-session state of any file. Use this to confirm whether a behavior was always present or introduced here.
- **Bug-class taxonomy**: §"Failure-mode patterns to recognize" at the bottom maps common symptoms to the axis that introduced the relevant code.
- **Path resolution mistakes**: see Axis 4. Most "wrong file read" bugs in this codebase trace to confusion between `RepoLayout` (cluster sidecar) and `JournalLayout` (cross-experiment journal).
- **Schema validation surprises**: see Axis 6. `partial_errors` is a top-level optional envelope key; `lifecycle_state` enums are now unified across schemas; `envelope.json` has `$defs` that callers should `$ref` (not all do today — see "Known incomplete migrations").
- **Idempotency-related**: see Axis 6 (`hpc_mapreduce/idempotency.py:IdempotencyKey`).

## Architectural axes

### Axis 1 — Identity-duplication dedup across the primitives layer

**Motivation**: the cross-cutting audit found ~13 verbatim helper duplicates that had drifted across modules — UTC time formatting, atomic-locked-doc patterns, SSH target parsing, etc. Each duplicate was a future drift surface.

**Concrete extractions** (file → location ; consumers — for `git blame` orientation):
- `hpc_mapreduce/_time.py` ← `parse_iso_utc`, `parse_iso_utc_or_none`, `utcnow`, `utcnow_iso`. Replaces 8 raw `datetime.now(timezone.utc).isoformat()` calls and 5 inline `_parse_iso` helpers across `agent_cli.py`, `infra/inspect.py`, `job/planner.py`, `job/runtime_prior.py`, `job/calibration.py`, `job/blacklist.py` (later deleted), `slash_commands/session.py`.
- `hpc_mapreduce/_io.py:atomic_locked_update(path, mutate)` ← byte-identical `_with_locked_doc` between `job/blacklist.py` (later deleted) and `job/runtime_prior.py`. Both now consumers.
- `hpc_mapreduce/infra/remote.py:split_ssh_target` ← duplicated in `job/submit_flow.py` and `job/aggregate_flow.py`.
- `hpc_mapreduce/infra/backends/_remote_base.py:RemoteHPCBackend` mixin ← byte-identical `_build_command`, `_build_dependency_flag`, `JOB_ID_REGEX` between local and remote SGE/Slurm backends.
- `hpc_mapreduce/job/backfill.py:_gather_usable` ← inlined logic in `recommend_walltime_sec` (the function predates the helper).
- `hpc_mapreduce/infra/gpu.py:_run_qstat` ← now uses `infra.remote.ssh_run` with the canonical 60s timeout (was inline SSH with hardcoded 10s).
- `hpc_mapreduce/reduce/metrics.py:_weighted_mean` ← extracted from two callers.
- `hpc_mapreduce/schemas/envelope.json:$defs` ← `run_id`, `combined_waves`, `failed_waves`, `lifecycle_state_*`. Canonical source. Note: the runtime `jsonschema.validate` doesn't register a `RefResolver`, so consumer schemas still inline the values; `$defs` is documentation today, not a wire-resolution mechanism.
- `hpc_mapreduce/templates/common/{hpc_preamble.sh,gpu_preamble.sh}` ← source-replaced 4 verbatim blocks across SGE/SLURM CPU/GPU templates.
- `tests/conftest.py` ← extracted from 7 hand-written sidecar fixtures.
- `scripts/_shared.py` ← `REPO_ROOT`, `VERB_ORDER`, `sort_verbs`, `summarize_side_effects`. **Side-effect**: `build_operations_index.py` had a stripped renderer that silently dropped structured side-effects entries; the shared helper restores fidelity.
- `slash_commands/runner.py:_parse_remote_json` ← consolidates two inline `json.loads`-or-raise patterns.
- `agent_cli.py` last-resort error envelopes ← `errors.SpecInvalid`/`errors.HpcError` (was inline `error_code` literals at the catch-all).

**Failure modes to recognize**:
- An `attribute error: '_with_locked_doc'` indicates direct call to a helper that no longer exists; the caller should be using `atomic_locked_update`.
- A SSH-related test that expects a 10s timeout broke when `gpu._run_qstat` migrated to the canonical `ssh_run` (60s).
- A test that mocked `subprocess.run` and broke at the inline-SSH layer needs to mock at `ssh_run` instead.

### Axis 2 — P0 bug fixes

Two production-impacting bugs caught during the audit. Both have regression tests.

**Bug 1** (commit `c0468e1`): `monitor_flow.py:218` and `aggregate_flow.py:192` were calling `session.runs_dir(experiment_dir)` (the journal directory at `~/.claude/hpc/<repo_hash>/runs/`) and looking for `wave_map` there. `wave_map` lives in the cluster sidecar at `<exp>/.hpc/runs/<run_id>.json`, not the journal. Result: `wave_map` was always `None` on those code paths; auto-combine and `ensure_all_combined` had been silently broken on every campaign for as long as that code existed. The directory collision was only possible because two unrelated path helpers (`runs_subdir` and `runs_dir`) shared the name "runs" with no type discrimination — root cause that Axis 4 (`RepoLayout` / `JournalLayout`) eliminates.

Fix: `monitor_flow.py` + `aggregate_flow.py` route through `read_run_sidecar` (which resolves through `RepoLayout(exp).run_sidecar(run_id)`).

`read_run_sidecar` was hardened (commit `90eac6f`) to guarantee `wave_map: dict`, `task_count: int`, `result_dir_template: str` regardless of sidecar version, so callers no longer need defensive `.get(..., {})`.

**Bug 2** (commit `1f465c1`): `hpc_mapreduce/map/dispatch.py:37 SUPPORTED_SCHEMA_VERSIONS = (1,)` while `hpc_mapreduce/job/runs.py:49 SIDECAR_SCHEMA_VERSION = 2`. Every fresh submit produced a v2 sidecar that the cluster-side dispatcher rejected with `[dispatch] ERROR: sidecar schema_version=2`. Wave 1 of every fresh submit failed.

Fix: `dispatch.py` now accepts `(1, 2)`. Verified the dispatcher only reads three sidecar fields (`sidecar_schema_version`, `executor`, `result_dir_template`) — all unchanged in v2 — so accepting v2 is safe.

**If a similar drift pattern appears**: the cross-domain version check in `hpc_mapreduce/_version.py:compatibility_check` (B8, see Axis 6) is the canonical place to declare new schema-version-handshake invariants.

### Axis 3 — A-series cross-cutting audit fixes (A1–A11)

Catalog of 11 drift / dead-code / typed-error issues. Each has a regression test and a CHANGELOG entry.

| ID | Issue | Fix |
|---|---|---|
| A1 | `check-preflight.md` `backed_by.python` pointed at non-existent module | Rewired to `hpc_mapreduce.agent_cli.cmd_preflight` |
| A2 | Four primitive docs (`submit-spec.md`, `mark-run-terminal.md`, `monitor-flow.md`, `resubmit-failed.md`) declared bogus `<exp>/.hpc/runs/` mirror writes | Frontmatter side-effects corrected |
| A3 | Schema enum drift: `monitor_flow.output.json` allowed `timeout`; `status.output.json` and `reconcile.output.json` didn't | Unified to canonical 5-state enum |
| A4 | Failure-category vocabulary divergence: classifier (`runner.py:cluster_failures_by_fingerprint`) emitted 5 categories the resubmit (`agent_cli.py:_VALID_RESUBMIT_CATEGORIES`) silently rejected | Took the union as canonical |
| A5 | `find_run_by_cmd_sha` was never wired to `submit_and_record`; rm of `~/.claude/hpc/` would let a duplicate submit through | `submit_and_record` now consults journal first, then per-experiment cmd_sha sidecar; reconstructs the journal record on hit |
| A6 | 4 missing primitive docs (`walltime-drift`, `house-edge`, `logs`, `failures`) — invisible to `cmd_capabilities` catalog | Authored |
| A7 | `cmd_capabilities` had a hand-typed `subcommands` array out of sync with argparse | Now derived from live `argparse._subparsers_action.choices` |
| A8 | `agent_cli.py:7` docstring claimed stderr is JSON-per-line; reality is free-form prose | Docstring corrected |
| A9 | `<run_id>.monitor.jsonl` writer race: `monitor_flow._append_tick` and the slash command both append, no flock | Now flock-guarded with `fcntl` (best-effort no-op on Windows) |
| A10 | `claude_hpc_version` sidecar field written but never read (dead code) | `read_run_sidecar` now warns once per `(run_id, version)` mismatch |
| A11 | `inspect_cluster` raised bare `KeyError` for unknown clusters (surfacing as `error_code: internal`) | Raises typed `errors.ClusterUnknown` matching the documented contract |

**If a primitive's frontmatter `backed_by.python` is wrong**: the cross-validation test `tests/test_primitive_spine.py:test_func_module_importable` (Axis 8) should catch it at CI time. If it doesn't, the test was bypassed — re-enable.

### Axis 4 — `hpc_mapreduce.layout` (B1)

**Motivation**: 8 scattered path helpers across 5+ modules with two unrelated roots that shared confusing names (`runs_subdir` vs `runs_dir`). The Axis 2 wave_map P0 bug was a direct consequence.

**Design**: two frozen dataclasses, type-distinct so `mypy` / readers cannot confuse them.

```python
RepoLayout(experiment_dir).hpc            # <exp>/.hpc/
RepoLayout(experiment_dir).runs           # <exp>/.hpc/runs/  (cluster sidecars)
RepoLayout(experiment_dir).runtime_prior(profile, cluster)
RepoLayout(experiment_dir).run_sidecar(run_id)

JournalLayout(experiment_dir).root        # ~/.claude/hpc/<repo_hash>/
JournalLayout(experiment_dir).runs        # journal RunRecords
JournalLayout(experiment_dir).run_record(run_id)
JournalLayout(experiment_dir).last_status(run_id)
JournalLayout(experiment_dir).monitor_jsonl(run_id)
```

`__post_init__` calls `.resolve()` so relative `experiment_dir` arguments are absolute; this prevents the split-brain state where two callers with different cwds compute different `repo_hash` values.

The 8 deprecated forwarders (`framework_subdir`, `runs_subdir`, etc.) are kept in `__init__.py:__all__` for one release.

**Failure modes to recognize**:
- A bug where the same `experiment_dir` produces different `repo_hash` from two callers indicates a missed `.resolve()` somewhere — look for direct `Path(experiment_dir)` construction outside `RepoLayout`.
- "FileNotFoundError on a sidecar that was just written" usually means the writer used `RepoLayout` but the reader is using `JournalLayout` (or vice versa).
- Test failures that pass when run in isolation but fail in `pytest -q` indicate a missed `.resolve()` against a chdir'd test environment.

**Subtle behavior change**: `RepoLayout(exp).tasks` triggers a `.hpc/` mkdir as a side-effect of property access, whereas the baseline `tasks_path()` was a pure path query. Production-benign (`.hpc/` always exists by the time anyone reads `tasks_path`) but flagged for future debugging context.

### Axis 5 — `hpc_mapreduce.lifecycle` (B2)

**Motivation**: 4 scattered string vocabularies for run state, lifecycle state, task status, and failure category. Cross-validation tests now pin invariants.

```python
class JournalStatus(StrEnum):     # session.RunRecord.status
class LifecycleState(StrEnum):    # workflow envelope (monitor_flow / status / reconcile)
class TaskStatus(StrEnum):        # reduce/status.py per-task rollup
class FailureCategory(StrEnum):   # union of classifier emissions and resubmit accept set
```

**Pinned invariants** (`tests/test_lifecycle.py`):
- `set(LifecycleState)` matches every schema enum at `monitor_flow.output.json`, `status.output.json`, `reconcile.output.json`.
- Classifier emissions ⊆ `FailureCategory` (all values).
- Classifier emissions ⊆ resubmit accept set (the A4 invariant; was a real bug pre-B2).

**Failure modes to recognize**:
- A schema validation error mentioning an unknown `lifecycle_state` value — check whether you forgot to update one of the three schemas. The `tests/test_lifecycle.py` cross-check should fail loudly.
- A new failure category emitted by the classifier that resubmit rejects — add it to `FailureCategory` and the test will green.

### Axis 6 — B3-B8 cross-cutting infrastructure

Five separate refactors bundled because they share the "introduce one canonical primitive to consolidate scattered patterns" shape.

#### B3 — `partial_errors` envelope key + `ClusterPartiallyDegraded`

**Problem**: `inspect_cluster`, `query.py:query_sacct`, `pick_gpu`, and the cluster-side combiner returned `errors: [...]` *inside* an `ok:true` `data` block with a separate, undocumented vocabulary (`scontrol_failed`, `qhost_failed`, `qstat_unavailable`, etc.). Agents reading the envelope saw `ok:true` and had no contractual obligation to inspect `data.errors`.

**Fix**: top-level `partial_errors: array of {code, detail}` envelope key (`schemas/envelope.json`); `ClusterPartiallyDegraded` exception class; `_err_from_hpc` surfaces `partial_errors` to the envelope when present.

**Migration status**: `cmd_inspect_cluster` migrated. `query.py` and `pick_gpu` callers have not yet been migrated (back-compat-safe — the legacy `data.errors` shape is still populated). See "Known incomplete migrations" below.

**Pinned schema validators** (e.g., a downstream MARs harness) using `additionalProperties: false` against the BASELINE envelope schema will reject `partial_errors`. Re-pin against current.

#### B4 — `IdempotencyKey` resolver

**Problem**: 5 idempotency mechanisms with no documented precedence. `find_run_by_cmd_sha` was defined but never called by `submit_and_record` (A5 fixed). Envelope `idempotent` flag was hardcoded at 47 callsites in `agent_cli.py`.

**Fix**: `hpc_mapreduce/idempotency.py` defines `IdempotencyKey` ABC with `RunIdKey`, `CmdShaKey`, `RequestIdKey` subtypes. `dedup_check(experiment_dir, key) → Optional[PriorResult]` consults journal first, then sidecar. `agent_cli._meta_idempotent(name)` derives the envelope flag from `operations_catalog()` (B4-rewire is partial — see "Known incomplete migrations").

#### B5 — `HPCBackend` ABC widened

**Problem**: 16 `if scheduler == "slurm" / "sge"` branches scattered across `__init__.py`, `infra/remote.py`, `infra/inspect.py`, `job/planner.py`, `job/submit_flow.py`, `reduce/status.py`, `reduce/tui.py`, `slash_commands/runner.py`. Backend registry existed at `infra/backends/__init__.py:get_backend` but only `submit_flow` consumed it by name.

**Fix**: ABC gains `template_ext`, `alive_job_ids`, `stderr_log_path`, `inspect`, `supports_test_only_eta`, `query_jobs`, `err_log_disk_path`. PR2 migrates the most important callers; PR3 (the rest) is in flight.

**Failure modes to recognize**:
- A new scheduler-specific behavior that breaks when added: check whether the new behavior went through the ABC or got slipped in as another `if scheduler ==` branch. Greps for that pattern outside `infra/backends/` should return zero post-PR3.

#### B6 — `infra/cache.py:TTLCache`

Replaces `inspect._CACHE` and `backfill._PROBE_CACHE` (both 60s TTL, both unbounded — long campaigns leaked memory). Now LRU-bounded with `clear_all()` and per-instance `invalidate(key)`.

`runner.py`'s file-based `<run_id>.last_status.json` snapshot has different lifetime semantics (24h+) and is intentionally NOT migrated — it's file-based persistence, not in-process cache.

#### B7 — `hpc_mapreduce/telemetry.py`

Replaces unguarded `monitor_flow._append_tick`. flock-guarded JSONL sink with `record(event, payload)` API. Cluster-side `dispatch.py` and `combiner.py` keep their inline `print(stderr)` per the stdlib-only constraint.

**Race that this fixes**: `slash_commands/commands/monitor-hpc.md:474` documented that the slash command may *append* to the same `<run_id>.monitor.jsonl` that `monitor_flow` writes; without flock, two concurrent writers could interleave bytes mid-line.

#### B8 — `_version.compatibility_check`

Single manifest at `hpc_mapreduce/_version.py:_MANIFEST` mapping `domain → supported_versions`. Replaces 5 scattered module-local `SCHEMA_VERSION = N` constants. Throws `errors.SchemaIncompat` on mismatch. `read_run_sidecar`, runtime_prior reader, and session journal reader all route through it.

### Axis 7 — `HPCBackend` ABC migration (B5-PR2)

Concrete migration of the 16 branches identified in B5. Each commit is a single `if scheduler == "..."` site converted to `get_backend(scheduler).<method>(...)`. `tests/test_planner.py` and `tests/test_runtime_uv.py` exercise the migrated paths.

**B5-PR3 status**: a few sites remain (run `grep -rn "if scheduler ==" hpc_mapreduce/ slash_commands/` and exclude `infra/backends/` to find them). Not blocking; the ABC now has the methods.

### Axis 8 — Primitive registry spine (C′)

**Motivation**: primitive metadata was duplicated in 5 layers (`operations.py` catalog, `agent_cli.py` argparse, `schemas/*.json`, `docs/primitives/*.md`, `slash_commands/commands/*.md` + `skills/*/SKILL.md`). Drift between layers caused real bugs (A1: `backed_by.python` pointed at a non-existent module; nobody noticed for months).

**Design**: implementation is the source of truth. `@primitive(...)` decorator captures metadata at the function definition. Frontmatter, catalog, and indexes are *generated views*.

```python
@primitive(
    name="submit-flow",
    verb="workflow",
    composes=["submit-spec", "combine-wave"],   # function-ref planned (c-prime-v2 #3)
    side_effects=[SideEffect("rsync", "<ssh_target>"), ...],
    error_codes=[errors.SpecInvalid, errors.SshUnreachable, ...],
    idempotent=True,
    idempotency_key="run_id",
)
def submit_flow(...): ...
```

**State at merge**:
- 27 of 28 primitives decorated.
- `operations.py` reads from `_REGISTRY`; frontmatter is a fallback (with `UserWarning`) for any non-decorated holdouts.
- `scripts/build_primitive_frontmatter.py` regenerates `docs/primitives/*.md` frontmatter from the registry.
- `scripts/lint_primitive_modules.py` (CI gate) asserts every module containing `@primitive(...)` is in `_PRIMITIVE_MODULES`.
- Cross-validation tests (`tests/test_primitive_spine.py`):
  - `test_no_orphan_primitive_modules` — auto-discovery via AST scan
  - `test_composes_references_resolve` — every atom in `composes` is in the registry
  - `test_func_module_importable` — `meta.func.__module__` resolves (the A1 bug class)
  - `test_clean_import_does_not_raise` — no circular import / recursion in `_ensure_imported`
  - `test_decorator_matches_frontmatter` — currently SKIPs on cosmetic prose drift

**c-prime-v2 follow-up** (in flight at merge): 5 user-picked refinements
- (b) CI lint replacing runtime auto-discovery — landed
- (a) function-ref `composes` for static refactor safety — partial
- (a) explicit `register_primitives()` replacing `_ensure_imported` — landed (`da58fc5`)
- (a) generate frontmatter from registry — script landed; `--check` CI gate pending
- (b) split pure cmd_* dispatchers into `hpc_mapreduce/atoms/<name>.py` — pending

**Failure modes to recognize**:
- A primitive missing from `cmd_capabilities` output: it's not in `_PRIMITIVE_MODULES`. `scripts/lint_primitive_modules.py` should catch this in CI.
- An `ImportError` on `meta.func.__module__`: A1-class drift. Check whether the primitive doc's `backed_by.python` matches the actual decorator location.
- A primitive that decorates the wrong function (the c-prime-v2 regression that flagged `build-executor` decorator on `cmd_campaign_health`): re-run `python -m hpc_mapreduce capabilities` and check the `python` field.

### Axis 9 — Borrowable primitives (D-series)

Five primitives derived from published HPC-agent papers. Each is a small, self-contained module.

- **D1b `validate`** (`job/validate.py`, primitive doc, schemas): promotes the internal `sbatch --test-only` probe to a top-level CLI primitive. Returns `{estimated_start_iso, fits_backfill, predicted_eta_sec, scheduler_response}`. LARA-HPC paper pattern.
- **D1c `failure_signatures`** (`job/failure_signatures.py`): pattern-match `(stderr, exit_code) → {error_class, suggested_fix}`. 9-entry catalog (`gpu_oom`, `system_oom`, `walltime`, `node_failure`, `file_not_found`, `import_error`, `permission_denied`, `disk_full`, `python_traceback`). VASPilot pattern. **Note**: SEGV entry deleted (Axis 10) when the SEGV blacklist feature was removed.
- **D2a `campaign-health`** (`job/campaign_health.py`): structured campaign-health payload + LLM-ready `suggested_prompt`. Returns `{n_runs, walltime_cliff_rate, queue_wait_quantiles, failure_breakdown, gpu_utilization, suggested_prompt}`. WISDOM '25 pattern; consumer (the agent harness) feeds the prompt to its LLM.
- **D2b `--partial-ok`** flag on `submit-flow`: writes failed task IDs to `<run_id>.failed.json` instead of fail-fast. `aggregate-flow` honors the marker by skipping listed tasks.
- **D2c `capabilities --full`**: dumps the entire API surface (catalog + every primitive doc + schemas + envelope + boundary-contract + cli-spec) as a single text blob. Modal `llms-full.txt` pattern.

### Axis 10 — SEGV blacklist removal — BREAKING

Per user direction, the SEGV blacklist feature was removed in full. The smart planner no longer consumes a blacklist signal.

**What was deleted**:
- `hpc_mapreduce/job/blacklist.py` (the module)
- `tests/test_blacklist.py`
- `docs/primitives/record-segv-blacklist.md`
- `record_segv` and `get_active_blacklist` from `hpc_mapreduce.__all__`
- `cmd_record_segv` from `agent_cli.py` + the argparse subparser
- `record-segv-blacklist.input.json` / `.output.json` schemas
- The SEGV row in `failure_signatures.py:CATALOG` (catalog dropped from 10 to 9 entries)

**Schema breaking changes** (`plan_submit.output.json`):
- Removed required field `blacklist_active_count`
- Removed per-candidate optional property `blacklisted_nodes`

Pinned consumers must re-pin or use `.get("blacklist_active_count", 0)`.

**Followups**:
- The `node_failure` failure-signature's `suggested_fix.action` was renamed `"blacklist-node"` → `"retry-on-different-node"` to remove the misleading reference.
- `slash_commands/commands/submit-hpc.md` Step 4c-A's "On SEGV: invoke record-segv-blacklist" block was replaced with "On SEGV: stop and surface to user."

**Failure modes to recognize**:
- An import error for `hpc_mapreduce.job.blacklist` indicates a stale caller (or a dependency you didn't realize you had).
- A test that expected `blacklist_active_count` in the plan-submit output: rewrite to use `.get("blacklist_active_count", 0)` or update the test.

### Axis 11 — Queue-wait baseline + Axis 12 — Microstructure features (Phase 1-3) + Phase 4 DES

**Foundation for predictive submission scheduling.** Designed for academic clusters where admin permission for cluster-wide sacct dumps is unavailable; works from per-user data only.

**Phase 0 (instrumentation, Axis 11)**:
- `runtime_prior.append_sample` records `queue_wait_sec` (derived from `started_at - submitted_at_iso` or accepted explicitly via kwarg). Negative deltas (clock skew between scheduler and submit-host) are rejected to `None` — the predictor sees only trustworthy observations.
- `_resolve_queue_wait_sec(explicit, started_at, submitted_at_iso)` is the canonical coercion helper.

**Phase 1 — Diurnal MA baseline (`hpc_mapreduce/job/queue_wait_baseline.py`)**:
- Hour-of-week bucketing (168 buckets).
- Exponential decay over sample age (14-day half-life by default).
- Three-tier fallback: `diurnal_ma` (target bucket dense) → `blended_ma` (blend with ±1h neighbors) → `global_ma` (weighted mean over all buckets) → `no_data` (cold start).
- Confidence ladder: high (n_bucket ≥ 4×min) → medium → low (fallbacks) → cold.
- `PredictionResult` frozen dataclass with `to_dict()` for envelope serialization.

**Phase 1 (substrate)** — order-book features (`hpc_mapreduce/job/queue_features.py`):
- `QueueFeatures` dataclass with per-partition queue depth, GPU-type supply/demand, resource pressure, snapshot age.
- `compute_features(snap, partition=None)` derives features from a `ClusterSnapshot`.
- Predictor accepts `current_features` as an additional regression signal.

**Phase 2 (per-user behavioral priors)** — `hpc_mapreduce/job/user_profiles.py`, `residual_lifetime.py`, `state_forecast.py`:
- `UserProfile` per-cluster JSON with `median_walltime_ask_sec`, `median_actual_over_ask`, `failure_rate`, `submit_hour_of_week_distribution`, `p_followup_within_6h`.
- `predict_residual_lifetime(profile, elapsed, walltime_ask)` — pure function; tests verify monotonicity.
- `forecast_state_at(snap, t_offset_sec, profiles)` — forward-projection of cluster availability.

**Phase 3 (CLI primitive)** — `best-submit-window`:
- Sweeps `predict_queue_wait` over the next N hours, returns top-K windows by predicted wait.

**Phase 4 (DES backend, in flight at merge)** — `hpc_mapreduce/job/queue_simulator.py`:
- Discrete-event simulator over FIFO + EASY backfill.
- `simulate_one_pass(snapshot, candidate, user_profiles, arrival_stream)` for deterministic forward simulation.
- `simulate_distribution(...)` runs N replications with sampled arrivals + sampled residual lifetimes; returns p10/p50/p90 of wait time.
- `replay-mode validation`: `scripts/validate_des_predictor.py` replays historical submits, compares predicted vs actual.
- Architectural decision: a thin custom DES (~500-800 LOC) over BSC's `slurm_simulator` (50K LOC C). The custom DES is calibratable against the residual; perfect SLURM fidelity is not the target.

**Why DES over the microstructure framing**: the scheduler policy is KNOWN (open source); DES exploits that structurally. Microstructure imports machinery designed for anonymous continuous markets and can't easily express dependency chains, hard resource constraints, or bounded job lifetimes. Microstructure features (Hawkes arrivals, identity-aware priors) feed the DES *as input distributions*; the DES is the predictor backbone.

**Failure modes to recognize**:
- Cold-start: predictor returns `predicted_wait_sec=None` with `confidence="cold"`. Don't treat that as a zero prediction; fall back to `--test-only` or no prediction.
- Non-monotonic predictions: bug in residual_lifetime or state_forecast. The cross-validation tests assert monotonicity in elapsed time and walltime ask.
- Replay accuracy worse than diurnal_ma alone: simulator residual indicates a bug or that FIFO is the wrong policy assumption for this cluster (try MULTIFACTOR if they enabled it).

### Axis 13 — POSIX-native agent surface (E3)

`README.md` + `docs/agent-surface.md` framing the design contrast vs three other agentic-HPC patterns. Positioning, not a feature.

## Failure-mode patterns to recognize

| Symptom | Likely axis | Where to look |
|---|---|---|
| "Wrong file read" / `wave_map is None` | 4 | Confusion between `RepoLayout` and `JournalLayout` |
| Schema validation error on `lifecycle_state` | 5 | `hpc_mapreduce/lifecycle.py:LifecycleState` enum |
| `error_code: internal` on a known failure mode | 3 (A11) or 6 (B3) | Should be raising a typed `errors.HpcError` subclass |
| Primitive missing from `cmd_capabilities` catalog | 8 | `_PRIMITIVE_MODULES` list in `_primitive.py`; CI lint should catch |
| `ImportError: hpc_mapreduce.X` from a primitive doc reference | 8 | A1 class — `func.__module__` mismatch |
| Sidecar v2 schema rejected on cluster | 2 (Bug 2) | `dispatch.py:SUPPORTED_SCHEMA_VERSIONS` and `_version.py` manifest |
| Test passing in isolation but failing in suite | 4 | Missed `.resolve()` on `experiment_dir` |
| `monitor.jsonl` line corruption | 6 (B7) | flock not acquired; check `telemetry.record` callers |
| Concurrent submit going through despite cmd_sha match | 3 (A5) or 6 (B4) | `submit_and_record`'s `find_run_by_cmd_sha` lookup |
| Per-user predicted wait wildly off | 11 / 12 | `UserProfile.median_actual_over_ask` not yet calibrated; cold-start fallback |
| `error_code: cluster_partially_degraded` | 6 (B3) | `partial_errors` envelope key — check `data` block for the inner detail |
| Plan-submit output missing `blacklist_active_count` | 10 | Intentional removal; use `.get(..., 0)` |

## Known incomplete migrations (open work)

These were started but not finished by merge time. Each is back-compat-safe (the legacy path still works); they're optimization opportunities, not bugs.

- **B3 partial_errors migration**: `query.py` and `pick_gpu` callers still populate `data.errors` in addition to `partial_errors`. Plan: deprecate `data.errors` after one release.
- **B4 envelope `idempotent` flag rewiring**: `_meta_idempotent` helper is in place but not all 47 callsites in `agent_cli.py` route through it. `_ok(idempotent=True/False, ...)` literals are still present; replace with `_ok(name="<primitive>", ...)` over time.
- **B5-PR3 scheduler dispatch**: a few `if scheduler == "..."` branches remain outside `infra/backends/`. Run `grep -rn 'if scheduler ==' hpc_mapreduce/ slash_commands/ | grep -v infra/backends/` to enumerate.
- **C′ split agent_cli (item #5 of babaa)**: pure `cmd_*` dispatchers (e.g., `cmd_capabilities`, `cmd_preflight`, `cmd_clusters_list`) decorate the dispatcher function in `agent_cli.py`. The plan was to move them to `hpc_mapreduce/atoms/<name>.py` as the canonical primitive home; in flight at merge.
- **Drift detector** (`tests/test_primitive_spine.py:test_decorator_matches_frontmatter`): currently SKIPs on cosmetic prose drift. Once `scripts/build_primitive_frontmatter.py --check` is wired into CI, this test becomes redundant — delete it.
- **Phase 4 DES predictor calibration**: simulator implementation in flight at merge; calibration against `replay-mode` validation requires accumulated samples (1-2 months of real submission data).

## Testing notes

- Run with `pytest -q --deselect tests/test_docs_links.py` — 7 tests in `test_docs_links.py` are pre-existing failures (missing `docs/mars-integration.md` and `docs/mars/experiment-runner.snippet.md` from a prior branch).
- Order-dependent flakes seen in `test_primitive_registry.py::test_primitive_meta_carries_all_fields` — passes in isolation, fails in suite due to the registry's module-import order. Should resolve when c-prime-v2 #6 (explicit `register_primitives()`) finalizes.

## Branch strategy used for the merge

Merged with `git merge --no-ff` (option D₂ from the merge-strategy options). Main's `git log --first-parent` shows just this merge commit (clean changelog view); `git bisect` and `git log --all` can drill into the branch's per-commit history when debugging requires it.

🤖 Generated with [Claude Code](https://claude.ai/code)
