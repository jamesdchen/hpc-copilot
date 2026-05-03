# Changelog

## Unreleased

### Removed — `hpc_mapreduce.campaign.run_campaign` asyncio loop and `defaults` callbacks

The closed-loop driver is now the slash-command surface itself: the
assistant repeatedly invokes `/submit-hpc campaign_id=<slug>` until
`tasks.total() == 0`. Concurrency is opt-in by firing more submits
before earlier ones land; the cluster scheduler runs them in parallel.
This eliminated three classes of complexity:

- **No asyncio mental model**: no `asyncio.run`, no `Awaitable`
  callbacks, no FIRST_COMPLETED gymnastics. Each iteration is a
  self-contained `/submit-hpc` invocation with the same approval
  prompts and failure surface as a one-shot submission.
- **No driver state to recover**: `run_campaign` previously needed
  `session.find_runs_by_campaign` + `await_completion` polling to
  re-discover in-flight runs after a network drop. The slash-command
  loop has nothing to recover — sidecars on disk are the only state.
- **No `submit_one` / `await_completion` / `should_submit` boilerplate**:
  the framework's `defaults.submit_via_cli` etc. existed only to wrap
  the CLI as async callables. Invoking the CLI directly works.

Removed:
- `hpc_mapreduce/campaign/loop.py` (`run_campaign`, `CampaignResult`)
- `hpc_mapreduce/campaign/defaults.py` (`submit_via_cli`,
  `poll_until_terminal`, `tasks_py_total_predicate`)
- `tests/test_campaign_loop.py`, `tests/test_campaign_defaults.py`,
  `tests/test_campaign_e2e.py`

Kept (the small surface that actually mattered):
- `campaign_id` field on submit specs and per-run sidecars.
- `HPC_CAMPAIGN_ID` env var threaded through scheduler templates.
- `hpc_mapreduce.reduce.history.prior(...)` for reading per-iteration
  reduced metrics back inside `tasks.py`.
- `hpc_mapreduce.campaign.campaign_dir(...)` for strategy-state
  placement (Optuna SQLite, PBT checkpoints).
- `hpc-mapreduce campaign list / status` CLI inspection.

For the migration story (every capability the asyncio loop offered has
an equivalent in the slash-command pattern, including K-in-flight,
FIRST_COMPLETED-style waits via parallel `Bash` calls, wall-clock
budget caps via env var + `tasks.py`, and headless overnight runs via
`/loop`), see `docs/campaign.md` and `slash_commands/commands/campaign-hpc.md`.

### Changed — `/monitor-hpc` is now silent-by-default; per-tick observations land in `.hpc/runs/<run_id>.monitor.jsonl`

Each `/monitor-hpc` tick used to emit a multi-line summary every time
(grid rollup table, `summary` counts, "no change since X" line, etc.).
At 5-min monitoring on a 24-hour run, that's ~290 ticks of narration
the user never reads. The skill now writes a structured record per
tick to a tick-log JSONL file and emits **nothing** to console unless
an action was taken (auto-resubmit, second-strike combiner failure),
the lifecycle flipped to a terminal state, or the user must
intervene (code bug, unknown failure).

When the user comes back and asks "what happened" / "status" /
"summarize", `/monitor-hpc summary` reads the JSONL and emits a
single digest. See `slash_commands/commands/monitor-hpc.md` Step 7.

### Added — Smart `/hpc-submit`: resource-quality-aware constraint planning

`/hpc-submit` previously chose its `--constraint=` and `--time=` from
static cluster config. The new path consults a snapshot of the cluster
plus per-(profile, cluster) runtime priors plus a SEGV blacklist, then
hands the scorecard to Claude for cost-model judgment over candidate
constraints.

Three independently shippable Python modules and one planner integrate
into a single `plan-submit` CLI subcommand:

- **`hpc_mapreduce.infra.inspect`** — `inspect-cluster --cluster <c>`
  returns a per-node snapshot (`AllocMem%`, `CPULoad%`, `Gres`,
  `GresUsed`, `ActiveFeatures`, `State`, plus a co-tenant list from
  `sacct -N` / `qstat`). `is_stressed` is set when `AllocMem >= 0.80`
  or `CPULoad/CPUTot >= 0.80` (both tunable). 60s in-process cache so a
  single submit cycle pays the SSH cost once. Both SLURM and SGE are
  supported.
- **`hpc_mapreduce.job.blacklist`** — append-only SEGV journal at
  `<repo>/.hpc/bad_nodes.<cluster>.json`. 7-day TTL, refreshed on
  repeat SEGVs. Atomic write under `fcntl.flock`. Evidence list capped
  at 5 most-recent entries per node. `record_segv()` is called by
  `/hpc-monitor` on `NODE_FAIL` / `exit -11`; `get_active()` is called
  by the planner with TTL filtering.
- **`hpc_mapreduce.job.runtime_prior`** — append-only sample log at
  `<repo>/.hpc/runtimes/<profile>.<cluster>.json`. `roll_up_quantiles()`
  groups by `gpu_type` and computes p50 / p95 / p99 / mean / n_samples,
  with optional `cmd_sha` filter so a `.hpc/tasks.py` change can
  invalidate stale priors.
- **`hpc_mapreduce.job.planner`** — `plan-submit --profile <p>
  --cluster <c>` combines all three into the scorecard JSON the slash
  command hands to Claude. When no priors exist, `needs_canary: true`
  and `canary_plan` describes the 1-task probe to seed the priors.
- **CLI**: three new subcommands on `hpc-mapreduce`: `inspect-cluster`,
  `runtime-prior`, `plan-submit`.
- **Slash commands**: `submit-hpc.md` gains a Step 4c describing the
  canary path and the cost rubric Claude applies. `monitor-hpc.md`
  gains a SEGV-detection branch that calls `record_segv()` so the
  blacklist is populated for future submits.

Tests in `tests/test_inspect_cluster.py`, `tests/test_blacklist.py`,
`tests/test_runtime_prior.py`, `tests/test_planner.py` cover the
parsing edge cases, TTL math, atomic write contract, quantile
computation with mixed sample counts, and an end-to-end planner shape
test using a fake cluster snapshot.

### Changed — `/status` slash command renamed to `/monitor-hpc`

The interactive Claude Code slash command at
`slash_commands/commands/status.md` is renamed to
`slash_commands/commands/monitor-hpc.md`. Users invoke it as
`/monitor-hpc` instead of `/status`. The CLI subcommand
(`hpc-mapreduce status`) is unchanged — only the human-facing slash
command was renamed; programmatic callers (MARs, scripts, and the rest
of the slash-command pipeline) continue to use the same JSON envelope
and exit-code contract.

Every cross-reference in the slash-command markdown, the docs
(`cli-spec.md`, `cli-contract.md`, `sync-checklist.md`,
`migration-from-hpc-yaml.md`), and `README.md` was updated. The skill
at `skills/hpc-status/` keeps its name; the MARs skill-name registry
(`_MARS_SKILL_NAMES`) is unchanged.

### Added — campaign helper layer (Optuna-recipe ergonomics)

Five small, strategy-blind additions surfaced by walking through the
end-to-end Optuna recipe in `docs/campaign.md`. None bind the framework
to a specific tuning library; they collapse boilerplate the previous
shape made every user write themselves.

- **`hpc_mapreduce.campaign.campaign_dir(experiment_dir, campaign_id)`** —
  canonical scratch directory `.hpc/campaigns/<cid>/`. Created
  idempotently. Reserved for strategy libraries to put state files
  (Optuna SQLite, PBT checkpoints, walk-forward cursor); the framework
  writes nothing inside.
- **`hpc_mapreduce.campaign.defaults`** — three curried-function defaults
  for `run_campaign`'s callbacks:
  - `tasks_py_total_predicate(experiment_dir)` — re-imports `tasks.py`
    each call and returns `total() > 0`.
  - `poll_until_terminal(experiment_dir, poll_interval_seconds=30)` —
    awaits one run via subprocess `hpc-mapreduce status` until the
    lifecycle state is terminal.
  - `submit_via_cli(spec_builder, experiment_dir)` — builds a spec via
    user callback, writes it to the campaign dir, shells out to
    `hpc-mapreduce submit`. Returns the new run_id.
  Together they collapse a typical campaign driver from ~80 lines to ~5.
- **`on_iteration_done` callback on `run_campaign`** — fires once per
  iteration with `(run_id, status, raw_metrics)` so strategy libraries
  can wire their "tell" call (Optuna's `study.tell()`, PBT's drop, etc.)
  without polling externally. Optional; the framework computes
  `raw_metrics` via the v2 sidecar pipeline when `experiment_dir` is
  provided. Empty dict for failed iterations.
- **`hpc_mapreduce.map.metrics_io.read_kw_env()`** — executor-side helper
  that returns `{lowercase_name: str_value}` for every `HPC_KW_*` env
  var the dispatcher exported. Stdlib-only; deployed alongside the
  executor.
- **Documented `cmd_sha` collision pattern** — for stochastic
  strategies (Optuna TPE, evolutionary), `resolve()` should include a
  unique-per-iteration value (e.g. `_optuna_trial_number`) so cmd_sha
  differs even when the strategy re-proposes the same params. Otherwise
  the framework dedups the submission silently. Doc-only fix.

### Added — closed-loop campaign primitive

The framework gains a small new primitive for adaptive iteration: a
**campaign** is a sequence of `/submit` invocations sharing a
`campaign_id` tag. The user's `.hpc/tasks.py` reads
`hpc_mapreduce.reduce.history.prior(experiment_dir, campaign_id)` at
module load to learn what prior iterations of the same campaign produced
and decide what to run next. Strategies (Optuna, RandomSearch,
walk-forward, PBT, …) live as Python libraries the user imports inside
their own `tasks.py` — the framework ships **zero** strategy code.

Surface area:

- **`campaign_id`** — first-class field on the v2 sidecar and on the
  journal `RunRecord`. Set via `--campaign-id` on submit specs;
  filterable via `session.find_runs_by_campaign`.
- **`HPC_CAMPAIGN_ID`** — env var forwarded by every scheduler template
  (SGE/SLURM × CPU/GPU). Read by the user's `tasks.py` and executor on
  the cluster.
- **`hpc_mapreduce.reduce.history`** — read-only accessor:
  - `prior(experiment_dir, campaign_id)` returns per-iteration reduced
    metric dicts, oldest-first. Pending iterations contribute `{}`.
  - `find_sidecars_by_campaign` and `result_dirs_for_sidecar` for
    callers that need the underlying primitives. None of these import
    `.hpc/tasks.py` (the loop's calling module), so no recursion.
- **`hpc_mapreduce.campaign.run_campaign`** — asyncio in-flight queue.
  Maintains *concurrency* live submits, awaits the next-finished one
  (FIRST_COMPLETED), repeats until the user's `should_submit` predicate
  flips to False or a wall-clock budget elapses. Fully IO-injected
  (`submit_one`, `await_completion`, `should_submit`); no fixed
  Strategy/Context Protocol.
- **`hpc-mapreduce campaign status` / `hpc-mapreduce campaign list`** —
  read-only CLI subcommands, JSON envelopes pinned by
  `schemas/campaign.output.json`.
- **`/campaign`** — slash command with the conversational interview;
  scaffolds a campaign-aware `tasks.py` from the recipes in
  `docs/campaign.md` (random search, Optuna ask/tell, walk-forward).

Resume semantics: sidecars on disk are the only durable state. After a
network drop or laptop sleep, re-running the loop re-discovers in-flight
runs via `find_runs_by_campaign`, polls them to terminal state, and
continues. No separate state file.

Failure semantics: a single iteration's failure surfaces via `on_event`
with an `error` field; the loop continues. Reissuing failed iterations
is the strategy library's call.

Out of scope: cluster-side queue (one array job draining a shared-FS
task queue), cluster-resident campaign driver, per-campaign retention.
All future work.

### Changed — `hpc.yaml` absorbed into the per-run sidecar

- **`hpc.yaml` is gone.** Every load-bearing field has moved into the
  per-run sidecar at `.hpc/runs/<run_id>.json` (sidecar schema bumped to
  v2): `cluster`, `profile`, `project`, `remote_path`, `resources`,
  `env`, `env_group`, `constraints`, `gpu_fallback`, `max_retries`,
  `runtime`, `auto_retry`, `aggregate_defaults`. Multi-stage DAGs move
  to `.hpc/stages.py` (Python file exposing `def stages() -> list[dict]`,
  validated against `hpc_mapreduce/schemas/stages.input.json`). Auto-retry
  caps that used to live in `hpc.yaml profiles[*].auto_retry` now have
  conservative hardcoded defaults in
  `slash_commands.runner.DEFAULT_AUTO_RETRY_POLICY` with per-run override
  via the sidecar. `cmd_aggregate` reads `aggregate_defaults` from the
  sidecar instead of the yaml.
- **No deprecation cycle.** Old `hpc.yaml` files in user repos are now
  silently ignored — the agent never reads them. Users who hand-edited
  `hpc.yaml` should re-run `/submit` once; the new sidecar will capture
  their resolved config from then on.
- **Deleted**: `_hpc_yaml_auto_retry`, `_hpc_yaml_aggregate_defaults`,
  `docs/schema.md`, `tests/fixtures/hpc_multistage.yaml`,
  `tests/test_hpc_yaml.py`. The README's `hpc.yaml` section is removed.
- **v1 sidecars on disk continue to load** with v2 keys backfilled to
  `None`, so existing journals are not broken.

### Changed — major refactor: `.hpc/tasks.py` task model

- **Collapsed the manifest + per-axis shim model into a single
  user-written `.hpc/tasks.py`** exposing `total()` and
  `resolve(task_id)`. The framework no longer ships a manifest format,
  a `_hpc_dispatch.json`, a `MANIFEST_ALIAS`, a chunking shim, a
  date-window shim, or any axis-enum spec block (`grid:`, `chunking:`,
  `backtest:`). Per-experiment task definitions live as plain Python
  the agent walks the user through writing once, committed to git in
  `.hpc/tasks.py`. Per-run state moves to `.hpc/runs/<run_id>.json`
  sidecars. Generic framework artifacts (`_hpc_dispatch.py`,
  `_hpc_combiner.py`, scheduler templates) ship with the package and
  are scp'd directly to the cluster's `.hpc/` by `deploy_runtime` —
  the experiment repo never holds a copy.
- **`error_code: "manifest_invalid"` renamed to `"spec_invalid"`** to
  match the new layer it covers (the per-run sidecar plus
  `tasks.py`). `HpcError` subclass `ManifestInvalid` renamed to
  `SpecInvalid`. No back-compat alias — there are no MARs consumers
  yet to break.
- **Build-executor types reduced to `plain`.** The `chunked`,
  `date-window`, and `shim` starter templates and matching
  `--type` argparse values are gone; per-task fan-out lives inline in
  `.hpc/tasks.py`, scaffolded by `/submit` Step 6 from the canonical
  reference at `hpc_mapreduce/templates/tasks_example.py`.
- **CLI flag rename.** `--manifest <path>` is now `--run-id <id>` on
  every subcommand that addresses a per-run record.

### Added — reliability / correctness

- **Stale-cache age field.** `status` and `list-in-flight` envelopes
  now carry `last_status_age_seconds` so consumers (humans + agents)
  can flag stale snapshots without changing freshness contracts.
- **Wave-aware `last_status`.** The on-cluster reporter now emits a
  `waves` rollup keyed by wave id with `{complete, running, pending,
  failed, unknown, total}` buckets. `record_status` and `reconcile`
  carry it into the persisted `last_status`. New `rollup_by_wave`
  helper in `hpc_mapreduce.reduce.status`.
- **`hpc-mapreduce logs` subcommand.** Fetches per-task stderr from the
  cluster: `--task-id 7,12,42` for explicit ids or `--all-failed` for
  every failed task. Falls back through earlier `job_ids` when the
  latest has no log. Removes a daily friction point.
- **`hpc-mapreduce failures` subcommand.** Triage tool: re-polls
  status, fetches stderr for failed tasks, strips volatile noise
  (timestamps, abs paths, pids, hex pointers), fingerprints the last
  non-empty line, and groups tasks sharing a fingerprint into clusters
  tagged with a category (`gpu_oom`, `walltime`, `import_error`, etc.).

### Added — robustness

- **Resubmit dedupe via `request_id`.** `resubmit_failed` now returns
  `(record, deduped, request_id)` and is idempotent on the (explicit
  or derived) `request_id`. A second call with the same spec returns
  `deduped: true` without incrementing per-task retry counters. A
  back-compat-default field `last_resubmit_request_id` was added to
  `RunRecord`.
- **`auto_retry` policy in hpc.yaml.** Per-category retry caps with
  optional resource multipliers (advisory). `hpc-mapreduce failures`
  annotates each cluster with `retry_advice = {policy,
  eligible_task_ids, blocked_task_ids}`. The framework never resubmits
  on its own — it surfaces eligibility; the caller decides. See
  `docs/schema.md` for the full shape.

### Added — MARs integration proposal package

- **MARs integration proposal package.**
  - `docs/mars-integration.md` — Bun.spawn env block, `error_code` →
    retry-policy mapping, troubleshooting flow for the silent-hang
    failure mode, journal-coexistence rules.
  - `docs/mars/experiment-runner.snippet.md` — paste-ready section for
    MARs's `agents/experiment-runner.md` covering preflight → submit →
    status → aggregate, decision rule for delegating to claude-hpc, and
    the full retry table.
  - `tests/test_docs_links.py` — drift guard ensuring every `error_code`
    and required env var mentioned in the proposal docs matches the
    code (`slash_commands/errors.py` and `capabilities.required_env`).
- **`capabilities` envelope additions** (additive, schema-compatible):
  - `mars_skill_paths` — absolute paths to bundled `skills/hpc-*/SKILL.md`
    so consumers can discover them without hardcoding the package layout.
  - `required_env` — env vars consumers must forward
    (`SSH_AUTH_SOCK`, `HPC_JOURNAL_DIR`, `HPC_CLUSTERS_CONFIG`).
- **README**: collapsed the "Using with MARs" section to a link to
  `docs/mars-integration.md`; kept the SSH-passthrough warning visible.

### Added — MARs compat Tier 2

- **`submit --from-meta`.** Overlay missing `profile` / `job_name` on
  the submit spec from `<experiment-dir>/meta.json` `experiment_id`
  via `setdefault` semantics — never overwrites caller-supplied
  values, silent no-op when `meta.json` is absent or missing
  `experiment_id`. Removes the manual overlay step from the MARs
  experiment-runner snippet.
- **`HPC_RUNTIME` job-env wiring.** New
  `slash_commands.runner.build_job_env(spec, base_env)` returns
  `base_env` augmented with `HPC_RUNTIME=uv` when `runtime: "uv"` is
  on the submit spec. The `/submit` slash command now constructs the
  `qsub` / `sbatch` env via this helper instead of inlining the gate
  logic. Closes the dangling end of Tier 1's `runtime: uv` work.
- **Doc refresh + drift guards.** Removed two stale claims from the
  MARs proposal docs (resubmit idempotence in
  `experiment-runner.snippet.md`; `uv run` "known gap" caveat in
  `mars-integration.md`). Added sentinel tests in
  `tests/test_docs_links.py` pinning the corrected wording so a
  future editor accidentally reintroducing either phrase fails CI.

### Added — MARs compat Tier 1

- **`meta.json` ingestion**. The `discover` envelope now includes a `meta`
  block with `experiment_id`, `seed`, `purpose`, and `tier` whenever a
  `meta.json` file exists at the experiment-dir root. Callers stop reparsing
  the file themselves. New helper `read_meta_json(experiment_dir)` is the
  single seam — silent on parse failures, since claude-hpc is not the place
  to validate MARs's schema beyond the fields it surfaces.
- **Tier detection**. `discover` surfaces `tier: 1 | 2 | null` derived from
  path layout: `probes/probe-*` + `probe.py` → 1, `runs/run-*` + `scripts/`
  → 2, otherwise null. New `detect_mars_tier(experiment_dir)` helper.
- **MARs layout discovery filter**. When `meta.json` is present at the
  experiment-dir root, executor discovery narrows to `scripts/` (Tier-2
  entrypoints) and the root-level `probe.py` (Tier-1) — never `src/`,
  honoring MARs's modules-only contract for that directory. Default
  behavior unchanged when the marker is absent.
- **`runtime: uv` profile.** Opt-in cluster-side `uv run` for every
  task command, with a `uv sync` preamble in all four shipped job
  templates (SGE CPU/GPU, SLURM CPU/GPU) gated on the `HPC_RUNTIME=uv`
  env var. Honors MARs's #1 invariant ("ALWAYS `uv run` … NEVER
  `pip`"). Templates exit 2 with a clear diagnostic when uv is
  missing — much clearer than running tasks with the wrong Python.
- **`schemas/discover.output.json`**: new file pinning the discover
  envelope's data shape (`executors` required, `meta` optional).
- **`schemas/submit.input.json`**: optional `runtime` field (`"uv"` or
  null) so the journal can record the runtime alongside the rest of
  the spec.

### Changed

- **`status`, `aggregate`, `reconcile` fail fast when `SSH_AUTH_SOCK` is
  unset.** Previously these subcommands hung indefinitely on auth — the
  most common Bun.spawn failure mode for orchestrators. They now emit
  `error_code: "ssh_unreachable"` (category `network`, `retry_safe: True`,
  exit 2) immediately. `submit` (journal-only) and `resubmit`
  (journal-only) are not gated.
- **`aggregate` gains framework-agnostic plumbing guarantees.** Three
  optional, additive checks help both human `/aggregate` users and
  agent CLI callers catch silent partial-data combines:
  - `--require-outputs <template>` — pre-combiner SSH check that every
    per-task output named by the template (with `{task_id}` placeholder)
    exists. Refuses to combine on partial data; surfaces a new
    `error_code: "outputs_missing"` (category `cluster`, `retry_safe: True`)
    listing the absent paths.
  - `--expect-output <path>` — post-combiner check that the declared
    artifact exists and (for `.json` paths) is parseable. A combiner
    that exits 0 but writes nothing now surfaces as `combiner_failed`
    immediately instead of producing a silent "successful" aggregate.
  - **Provenance** — the success envelope's `data` block always carries
    a `provenance` object: `{run_id, wave, profile, cluster,
    combined_at}`. When `--expect-output` is set, claude-hpc also
    writes a `_provenance.json` sidecar next to the output on the
    cluster (best-effort; envelope is the source of truth).
- **`hpc.yaml` defaults for the new aggregate flags.** Set
  `results.require_outputs` and `results.expect_output` once per profile
  to enforce the precondition/postcondition automatically on every
  aggregate. Explicit CLI flags override hpc.yaml. The
  `slash_commands/commands/aggregate.md` prompt now points users at
  this.

## 0.2.0 — 2026-04

Major refactor adding agent-facing CLI alongside the existing Claude Code slash
commands. Both surfaces share the same atomic-ops layer at
`slash_commands/runner.py` so cross-surface state stays consistent.

### Added

- **`hpc-mapreduce` CLI** (the agent surface). Subcommands: `submit`, `status`,
  `aggregate`, `reconcile`, `resubmit`, `preflight`, `discover`, `expand-grid`,
  `list-in-flight`, `clusters list|describe`, `capabilities`, `build-executor`.
  Stdout is a single-line JSON envelope; stderr is JSON-per-line log records.
  Exit codes: 0 ok, 1 user error, 2 cluster/network, 3 internal. Full schema
  in `docs/cli-spec.md`; runtime-validatable JSON Schemas under
  `hpc_mapreduce/schemas/`. Both `python -m hpc_mapreduce <cmd>` and
  `hpc-mapreduce <cmd>` work.
- **`/preflight` slash command** — health check matching the CLI subcommand,
  for Claude Code parity.
- **MARs SKILL.md files** under `skills/hpc-*/SKILL.md`. Drop-in workflow
  guidance for MARs orchestrator agents that invoke the CLI via the Bash tool.
- **Typed exception hierarchy** at `slash_commands/errors.py` (`HpcError` base
  with `error_code`, `retry_safe`, `category`, `remediation`). One source of
  classification, two presentations: CLI maps to envelope; slash commands let
  Claude format for the human.
- **SSH connection multiplexing** in `hpc_mapreduce/infra/remote.py`
  (`ControlMaster auto`, `ControlPersist 10m`). First call to a cluster opens
  the master socket; subsequent calls reuse it (~50ms vs ~500ms+). Opt-out
  via `HPC_NO_SSH_MULTIPLEX=1`.
- **Idempotent `submit`**. `submit_and_record` now returns
  `tuple[RunRecord, bool]`; the bool is `deduped`. Replaying a submit with the
  same spec returns the existing record and the cluster does not see duplicate
  qsub/sbatch calls. Removes a real correctness footgun for any caller that
  retries on transient network errors.
- **Last-status cache file** `<run_id>.last_status.json` written next to the
  journal record by `record_status`. Any consumer can read it for cheap
  cached state without re-issuing an SSH call.
- **Env-configurable state directories**:
  - `HPC_JOURNAL_DIR` overrides the journal location (default: `~/.claude/hpc/`).
  - `HPC_CLUSTERS_CONFIG` overrides the clusters.yaml path (default: shipped
    in the package).
- **`__version__`** on the `hpc_mapreduce` package, `--version` flag on the CLI.
- **`py.typed`** marker — `hpc_mapreduce` ships type hints to mypy/pyright.
- **New docs**: `docs/cli-spec.md`, `docs/config-precedence.md`,
  `docs/sync-checklist.md`.

### Changed

- **Renamed `agent/` → `slash_commands/`.** Disambiguates from MARs' own
  `agents/` concept. The directory is Claude Code slash-command glue, not an
  LLM agent. All Python imports `from agent import ...` → `from slash_commands
  import ...`. Plugin manifest at `.claude/commands/setup_hpc.md` updated.
  In-flight runs and on-cluster jobs are unaffected by the rename.
- **Renamed `/monitor` slash command → `/status`.** Verb collision with the
  built-in Claude Code `Monitor` tool that MARs orchestrators have available;
  the new name is also more accurate (one-shot snapshot, not a streaming
  watcher). CLI matches: `hpc-mapreduce status`. Existing in-flight runs and
  journal records pick up the rename without migration. Existing standalone
  Claude Code users will need to retrain on `/status`.
- **Relocated `config/clusters.yaml` → `hpc_mapreduce/config/clusters.yaml`**
  and `templates/{executor_template,chunking_shim,date_window_shim,shim_template}.py`
  → `hpc_mapreduce/templates/starters/`. Required for `pip install` from a
  wheel to ship with config and starter templates. **File contents are
  byte-identical** — only paths changed. Anyone who forked and customized
  these files will need to re-apply their edits at the new paths.
- **`_PACKAGE_ROOT` repurposed.** Previously the repo root; now points at the
  `hpc_mapreduce/` package directory itself. Affects path resolutions like
  `_PACKAGE_ROOT / "config" / "clusters.yaml"` (still works because the file
  moved into the package). External users importing `_PACKAGE_ROOT` to
  resolve paths in their own forks: audit your usages.
- **`submit_and_record` return type** changed from `RunRecord` to
  `tuple[RunRecord, bool]`. Callers must unpack: `record, deduped = ...`.
- **`pyproject.toml` packages discovery** changed from `["hpc_mapreduce*",
  "agent*"]` to `["hpc_mapreduce*", "slash_commands*"]`. Added
  `[project.scripts]` for the `hpc-mapreduce` console entry, and
  `[tool.setuptools.package-data]` so wheels bundle config + templates +
  schemas.

### Not changed (deliberately)

- **No MCP server.** MARs agents invoke external code through the Bash tool;
  a CLI is the right shape for that pattern. MCP would require adding a
  wrapper tool inside MARs. Revisit only if MARs itself shifts to
  surfacing MCP servers as first-class agent tools.
- **No cancel/abort subcommand.** `settings.json` denies `scancel`/`qdel`.
  If you decide an experiment is bad, stop waiting on it; cluster jobs run
  to walltime.
- **No local-execution backend.** claude-hpc is the HPC-on-cluster path;
  MARs already iterates locally via uv/Docker.
- **No deprecation shim for old `agent.*` imports.** Standalone users invoke
  the package via slash commands (which we update atomically) or the new CLI;
  external scripts importing `agent.*` directly do a one-time migration to
  `slash_commands.*`. The version bump signals the break.

### Migration notes for current standalone users

A user who pulls 0.2.0 and continues using claude-hpc as a Claude Code plugin
will see:

- **All four legacy slash commands work identically** modulo the verb rename:
  `/submit`, `/status` (was `/monitor`), `/aggregate`, `/build-executor`. New
  `/preflight` is added.
- **In-flight runs** at `~/.claude/hpc/<hash>/runs/*.json` continue to load
  unchanged — `schema_version` stays at 1.
- **Cluster-side jobs** already submitted run to completion as before;
  `dispatch.py` and `combiner.py` are untouched.
- **Customized `config/clusters.yaml`** at the old location: re-apply at
  `hpc_mapreduce/config/clusters.yaml`, OR set `HPC_CLUSTERS_CONFIG=/path/to/yours`.
- **Customized templates** at `templates/*.py`: re-apply at
  `hpc_mapreduce/templates/starters/*.py`.
- A user mid-conversation in Claude Code during the upgrade may hit one
  transient error as the model attempts to call the old `agent.*` path.
  Restarting the session fixes it.
