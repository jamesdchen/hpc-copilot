# Changelog

## Unreleased

### Changed — `docs/` reorganized; new top-level `README.md` and workflow doc

The `docs/` tree moves from a flat 11-file bag into four purpose-specific
subdirs plus a top-level navigation index:

```
docs/
├── README.md                             (NEW; nav map)
├── workflows/                            (NEW)
│   ├── memory-across-campaigns.md       (NEW; interview ↔ recall flow)
│   ├── campaign.md                      (mv from docs/)
│   ├── mars-integration.md              (mv from docs/)
│   ├── migration-from-hpc-yaml.md       (mv from docs/)
│   └── mars/experiment-runner.snippet.md (mv from docs/mars/)
├── reference/                            (NEW; wire contracts)
│   ├── cli-spec.md                      (mv)
│   ├── cli-contract.md                  (mv)
│   ├── agent-surface.md                 (mv)
│   ├── boundary-contract.md             (mv)
│   └── config-precedence.md             (mv)
├── internals/                            (NEW)
│   ├── queue-wait-predictor.md          (mv)
│   └── sync-checklist.md                (mv)
├── primitives/                           (kept; hybrid auto/hand)
└── generated/                            (NEW; whole-file auto-gen)
    └── operations.md                    (mv from docs/)
```

`docs/primitives/` stays at top level because it's hybrid (frontmatter
auto-generated, body hand-written). `docs/generated/` is reserved for
whole-file auto-generated content; the only file there today is
`operations.md`, which gains an `<!-- AUTO-GENERATED. DO NOT EDIT BY
HAND. -->` sentinel at the top.

The `docs/README.md` enumerates the layout, points to entry-point docs
by audience (new user / MARs integrator / wire-contract reader / primitive
lookup), and tabulates which files / sections are auto-generated.

`docs/workflows/memory-across-campaigns.md` documents the
`interview` → `recall` feedback loop end-to-end: what `interview.json`
captures, the two-mode (validate / generator) operation of the interview
primitive, the five typed `task_generator` shapes, the three rollup tiers
recall returns, and the `~/.claude-hpc/config.json:experiment_roots`
default-root config.

Root `README.md` updated:
- Agent CLI block adds `interview` and `recall`
- New "Memory across campaigns" subsection under "How It Works" linking
  to the workflow doc
- Configuration section adds `~/.claude-hpc/config.json` entry

Touch-points: 47 files updated for path rewrites (skills, slash commands,
build scripts, tests, schemas, source comments). Build scripts updated:
`scripts/build_operations_index.py` writes to `docs/generated/operations.md`;
the others stay pointing at `docs/primitives/`.

### Changed — `recall` projection broadened, rollup tiers added, config-driven roots

The `recall` primitive grew from "list past campaigns" into the full
memory layer for next-interview grounding.

**Per-campaign projection** (Fix A) — `recall` summaries now include
`budget`, `abort_if`, `cluster_target`, and `task_generator: {kind, params}`
in addition to the existing identity / metadata fields. These are the
prior-decision fields the next interviewer would otherwise have to
re-read every interview.json for. `notes` and `transcript` stay out of
the projection — too verbose; the calling agent re-reads
`interview.json` directly when it needs them.

**Rollup tiers** (Fix B) — every recall response now carries a
`rollup` block with cross-campaign aggregations.

* **Tier 1 (always-on)** — `count`, histograms over `task_kind` /
  `operator` / `produced_by_kind` / `task_generator.kind` / `cluster`,
  `task_count` quantiles (linear-interp p50/p95/min/max),
  `materialized_at` envelope (earliest/latest). Computed from the same
  data already projected; no extra IO.
* **Tier 2 — `--include-runtime`** — walks each matched campaign's
  `.hpc/runtimes/*.json` files. Aggregates `elapsed_sec` (quantiles +
  n_samples) and `failure_rate` from `exit_code != 0` across every
  dispatched task across every matched campaign. Reports
  `campaigns_with_no_runtime` so the caller sees how much of the
  matched set was uninformative.
* **Tier 3 — `--include-generator-stats`** — buckets matched campaigns
  by `task_generator.kind` and reports observed parameter envelopes:
  `numeric_logspace` / `numeric_linspace` get `param_envelopes` (low /
  high / n ranges per parameter name); `cartesian_product` gets
  `axis_value_unions` (every value seen on each axis name);
  `enumerated` / `items_x_seeds` get count only. Most useful with
  `--task-kind` also set so buckets aren't noisy.

Observed ranges only — no recommendations. Recall is the memory layer;
reasoning over the ranges stays in the calling agent.

**Config-driven default `--root`** (Fix C) — `--root` is now optional.
When omitted, the primitive falls back to
`~/.claude-hpc/config.json:experiment_roots` (a JSON file with an
`experiment_roots: [path, …]` field). Both empty raises `spec_invalid`
with a clear message — no implicit cwd default. Multi-root support
(`recall_campaigns(roots: list[Path], ...)`) means the config can list
multiple campaign trees and they're walked together.

### Added — `recall` primitive: query past interview.json files

`hpc-mapreduce recall --root <experiments-dir>` walks the tree for
`interview.json` files (produced by the interview primitive), filters
by `--task-kind` / `--operator` / `--since` (ISO-8601), and returns
recency-sorted summaries (goal, task_kind, task_count, operator,
materialized_at, cmd_sha). Substrate for "show me my last 5 LR sweeps"
prompts in the next interview — closes the memory loop between
campaigns.

Read-only and idempotent. No persistent index; the operator passes the
experiments root explicitly. Malformed `interview.json` files are
skipped silently. Hard-capped at 10K files per scan.

### Added — `interview.task_generator`: typed materializer for tasks.py

The `task_generator` field in `interview.input.json` is now a typed
`oneOf` (was a reserved bare-bones placeholder). When intent supplies a
`task_generator`, the primitive generates tasks.py from the recipe
instead of consuming an agent-written one. Five typed shapes:

- `enumerated` — items list verbatim (most agnostic; covers eval, RL,
  benchmark, data-shard campaigns).
- `cartesian_product` — full cross-product over named axes.
- `items_x_seeds` — items × seeds with `seed` merged into each item dict.
- `numeric_logspace` — `param` swept geometrically over `[low, high]`.
- `numeric_linspace` — `param` swept arithmetically over `[low, high]`.

The expected task count is computed pre-flight from the recipe and
cross-checked against `intent.task_count` *before* any disk write — a
recipe-vs-count mismatch never leaves a partial tasks.py behind.
Generator mode is byte-equivalently idempotent on re-run; an operator
who hand-edits the produced tasks.py drops `task_generator` from the
next intent and the primitive flips to validate-mode for the edited file.

### Added — `interview` primitive: persist campaign intent alongside tasks.py

The interview between hpc-agent and either MARs or a human now produces
a structured artifact (`<campaign-dir>/interview.json`) instead of only
chat-context that's gone the moment the campaign starts.

- `hpc-mapreduce interview --spec <intent.json> --campaign-dir <dir>`
  validates an agent-written tasks.py against the recorded intent
  (asserts `tasks.total() == intent.task_count`, fingerprints with
  `cmd_sha`, samples `resolve(0)`/`resolve(n//2)`/`resolve(n-1)` as a
  dry-resolve preview), then persists the intent verbatim plus a
  `_materialized` block to interview.json. When intent supplies
  `cluster_target` or `budget`, meta.json is merged-into (existing
  operator-set keys win on conflict; `total_tasks` is always derived
  from `tasks.total()`).
- Schema (`schemas/interview.input.json`) is deliberately bare-bones:
  required fields are `goal`, `task_count`, `produced_by`. Optional:
  `task_kind` (free-text recall tag), `budget` (opaque dict so units
  match the campaign — gpu_hours, cpu_hours, task_count, credits),
  `abort_if`, `cluster_target`, `transcript`, `notes`. The schema does
  *not* enumerate search-space shapes (logspace / grid / seeds_x); doing
  so would narrow the existing experiment-agnostic `tasks.py` contract
  to ML-hyperparameter sweeps. The reserved `task_generator` field is a
  placeholder for an opt-in typed materializer (not implemented).
- Spine for future `cmd_recall`: interview.json captures provenance
  (`{kind: mars|human, session_sha, operator, at}`) and a `cmd_sha`
  fingerprint, so future "show me my last 5 LR sweeps" queries can index
  past campaigns by `task_kind` and surface their typed parameters.

### Changed — folded slash_commands Python runtime into claude_hpc

The atomic-ops layer (runner.py), journal storage (session.py), and
typed exception hierarchy (errors.py) moved out of `slash_commands/`
into `claude_hpc/`:

- `slash_commands/runner.py`  → `claude_hpc/orchestrator/runner.py`
- `slash_commands/errors.py`  → `claude_hpc/errors.py`
- `slash_commands/session.py` → `claude_hpc/_internal/session.py`

Plus:

- `claude_hpc/operations.py`  → `claude_hpc/_internal/operations.py`
  (framework-internal plumbing, not user-facing)

The motivation is layering: `claude_hpc/` is the framework, `slash_commands/`
is the human-UX surface. Pre-fold, 7 framework files imported FROM
`slash_commands/`, which is upside-down. Post-fold, `claude_hpc/`
is self-contained.

`slash_commands/` retains its directory and `__init__.py` so the
markdown command templates (`slash_commands/commands/*.md`) still
ship as package data. Users who clone the repo and run `claude code`
inside it pick up the slash commands; the `/setup_hpc` flow still
copies them into `~/.claude/commands/` for global install.

Imports updated across ~10 framework files and the test suite.
Primitive frontmatter `backed_by.python` paths regenerated. No
behavior changes — purely a rearrangement.

### Changed — repository layout switched to PyPA src layout

Both top-level Python packages now live under `src/`:

- `claude_hpc/` → `src/claude_hpc/`
- `slash_commands/` → `src/slash_commands/`

Import names are unchanged (`import claude_hpc`,
`import slash_commands.runner`); only the on-disk layout moved.
`pyproject.toml` declares `[tool.setuptools.packages.find].where =
["src"]` and `[tool.mypy].mypy_path = ["src"]` so the editable install
and type-checker continue to resolve the packages by import name.

The src layout prevents the "import works from cwd without
`pip install -e`" footgun, which had bitten us twice.

### Removed (BREAKING) — `hpc_mapreduce` deprecation shim

The `hpc_mapreduce` shim package, added when the package was renamed
to `claude_hpc` in the previous release, has been removed. Any code
still importing `hpc_mapreduce.X` must update to `claude_hpc.X`.

The CLI binary `hpc-mapreduce <subcommand>` is unchanged — it was
always provided via `[project.scripts]` pointing at
`claude_hpc.agent_cli:main`, not via the shim. MARs and any other
agent harness that shells out to the binary needs no changes.

The shim's job was to give one release of grace for downstream
imports; that release has elapsed. Removing it eliminates the
`DeprecationWarning` pollution at every import and simplifies the
package layout.

### Added — walltime arbitrage and auto-daisy-chain (PR-C)

Two more survival defenses for the campus user submitting low-priority
jobs to clusters where higher-priority jobs consume most of the
resources. Both features fit the same pattern as PR-A and PR-B: they
don't make jobs more efficient — they help the campus user's *own*
jobs survive structural disadvantage. Both default-on but only fire
when they're safe.

**Cold-start walltime arbitrage.** A nominal walltime ask of 4:00:00
collides with every other 4:00:00 ask — the round numbers every
well-funded job requests are also the slots backfill schedulers
reserve. Asking 3:45:00 instead fits in backfill shadows the
4:00:00 jobs don't reach. The new helper
`claude_hpc.forecast.walltime_arbitrage.arbitrage_walltime`
subtracts 15min and floors to a 5min boundary; below a 1h floor the
ask passes through unchanged so short tasks aren't cliff-killed.
The planner (`plan_submit`) applies the trim only when the
`--test-only` lattice probe couldn't pin a winner (no priors path);
the lattice path supersedes arbitrage when priors exist. Per-cluster
opt-out via `walltime_arbitrage: false` in `clusters.yaml`. The plan
output gains a `walltime_arbitraged_from: <int_sec> | null` field
carrying the original ask when the trim fired.

**Auto-daisy-chain for tasks exceeding the cluster's hard ceiling.**
A walltime ask exceeding the cluster's hard scheduler ceiling fails
outright. Auto-daisy-chain splits the ask into N segments where each
segment N+1 holds on segment N (`--dependency=afterany:<id>` on
SLURM, `-hold_jid <id>` on SGE — `afterany` deliberately so a
preempted segment's exit-130 from PR-A still triggers segment N+1).
The trigger fires when the ask exceeds `max_walltime_sec - 1h` (the
1h buffer absorbs queue-wait variance between segments). No segment
cap — a 7-day task on a 24h cluster becomes ~8 segments; bounded
only by checkpointing actually working.

The chain is **default-off when checkpointing isn't detected** so we
don't silently waste compute. The new
`claude_hpc.orchestrator.checkpoint_detect.detect_checkpointing`
helper walks past run output dirs (`<exp>/.hpc/runs/*/result_dirs`)
for files matching `checkpoint*`, `*.ckpt`, `state*.pkl`, `last*.pt`,
`latest*.pt`, `model*.{joblib,pkl,pt}`, `epoch_*.{pt,pkl}`. Returns
True only when a past run of `(profile, cluster)` actually produced
checkpoint-shaped files; False on no past runs, no matching files,
or any error. With detection False, the planner emits an explanatory
error telling the user how to opt in (add checkpointing OR set
`auto_daisy_chain: true` in clusters.yaml).

Per-cluster controls in `clusters.yaml`:

- `max_walltime_sec: <int>` — hard scheduler ceiling. Default 24h
  (Hoffman2 highp); USC CARC Discovery main is 48h. Required for
  chain-decision math.
- `auto_daisy_chain: true` — always chain (skip checkpoint scan).
- `auto_daisy_chain: false` — never chain on this cluster (kill
  switch). The "exceeds max walltime" error fires unmodified.
- (key absent) — defer to `detect_checkpointing` (the safe default).

The plan output gains two more fields: `daisy_chain_segments: <int> |
null` (segment count when chained) and `daisy_chain_dep_jobids:
<list[str]> | null` (the actual scheduler dep jobids; populated
post-submit by `submit_flow`, null at plan time).

New typed validator helpers in `claude_hpc.infra.clusters`:
`get_walltime_arbitrage`, `get_auto_daisy_chain`,
`get_max_walltime_sec`. Each rejects wrong-typed yaml values
(`walltime_arbitrage: "yes"` is a string, not a bool — fails loudly
at load time rather than silently disabling the feature).

### Added — dispatch resilience for the campus user (PR-A)

Three changes that help low-priority "campus user" jobs survive a
hostile shared HPC environment, where higher-priority work routinely
preempts the user's tasks. None of these change framework-internal
behaviour for non-preempted runs.

* `claude_hpc/mapreduce/dispatch.py` now traps `SIGTERM` from the
  scheduler. The handler logs `[claude-hpc] SIGTERM received;
  cluster preemption imminent` to stderr, writes
  `preempt: {at: <utcnow_iso>, grace_sec: <int>}` to the per-task
  entry of `<exp>/.hpc/runs/<run_id>.json`, forwards `SIGINT` to the
  executor subprocess so its except blocks run during the cluster's
  preemption window, waits up to `HPC_PREEMPT_GRACE_SEC` (default
  25s) for clean exit, then `sys.exit(130)`. Marks the run as bumped
  (not failed) so the agent harness can resubmit cleanly without
  surfacing a real failure to the user. Stays cluster-side
  stdlib-only.
* `claude_hpc/mapreduce/dispatch.py` skips invoking the executor on
  resubmit if `result_dir/metrics.json` already exists with non-zero
  size — the campus user resubmits a preempted task without redoing
  already-completed work. Convention: executors that don't call
  `claude_hpc.mapreduce.metrics_io.write_metrics` won't get
  free skip-on-resubmit.
* `slash_commands/errors.Preempted` is the new typed exception
  (`error_code: preempted`, `category: cluster`, `retry_safe: True`).
  Wired through the agent envelope (`error_code` enum in
  `claude_hpc/schemas/envelope.json`), the failure-signatures catalog
  (exit-code 130 → `error_class: preempted`, `suggested_fix: {action:
  resubmit-preempted}`), and `cmd_failures` (`preempted_count` /
  `preempted_task_ids` surfaced at the data top level). Also added to
  the canonical `FailureCategory` StrEnum so classifier-emits ⊆
  resubmit-accepts.

### Added — template defenses for low-priority campus jobs (PR-B)

Three survival defenses for the campus user submitting low-priority
jobs to UCLA Hoffman2 (UGE) and USC CARC (Slurm) where higher-priority
jobs consume most of the resources. These don't make jobs more
efficient — they help the campus user's *own* jobs survive structural
disadvantage.

**Thread caps in the shared template preamble.** All four templates
(SGE/SLURM × CPU/GPU) now source `claude_hpc/mapreduce/templates/common/hpc_preamble.sh`,
which exports `OMP_NUM_THREADS=1` plus the four sibling caps for MKL,
OpenBLAS, NumExpr and vecLib. Without this, a campus user running
NumPy on a 1-core allocation gets BLAS spawning 16 threads, blows past
the cgroup CPU limit, and gets killed by the OOM daemon. Per-experiment
override via `$HPC_OMP_NUM_THREADS=N` (and the per-library siblings
`HPC_MKL_NUM_THREADS` / `HPC_OPENBLAS_NUM_THREADS` / `HPC_NUMEXPR_NUM_THREADS`
/ `HPC_VECLIB_NUM_THREADS`) in the spec's `job_env`. The CPU/GPU array
templates' existing re-exports of `OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK`
/ `$NSLOTS` still take precedence for the multi-threaded case, since
they run after the preamble.

**NFS staging via `$LOCAL_DATA_DIR`.** When `$HPC_NFS_DATA_DIR` is set
in the cluster job's env, the preamble rsyncs that directory into
node-local SSD (`$SLURM_TMPDIR` / `$TMPDIR` / `/tmp`) and exports
`$LOCAL_DATA_DIR` for user code to read from. The contract is the
variable name: user executors should prefer `$LOCAL_DATA_DIR` when
set. Without this, a 200-task array all `open()`ing the same NFS files
at once is the textbook way to get the array throttled — local SSD
reads are ~100× faster and scale per-node, not per-cluster. Strictly
opt-in: users without an NFS dataset pay nothing.

`clusters.yaml` gains an optional `nfs_data_dir:` field per cluster.
When set, `submit_flow` injects it as `HPC_NFS_DATA_DIR` into the
cluster job's env so the staging block fires automatically. Caller-
supplied `job_env` wins via `setdefault`, so per-experiment dataset
overrides still work.

**Cold-start memory buffer in the smart planner.** When no usable
runtime prior exists for `(profile, cluster, gpu_type)`, the user's
`--mem` ask in MB is grown by `(1 + cold_start_mem_buffer)`, then
floored to the existing `floor_mb` minimum, so the OOM daemon doesn't
bump the campus user's brand-new run mid-write and leave a corrupt
result dir behind. This is the cold-start "I have no
idea how much memory you'll use" headroom; the smart planner takes
over once you have ≥5 successful samples per `(profile, cluster,
gpu_type)`, at which point the quantile-based shrink owns and the
buffer is no longer applied (the priors already encode the right
safety margin via `walltime_drift` calibration).

`clusters.yaml` gains an optional `cold_start_mem_buffer:` field per
cluster (default `0.15` = 15%). Set to `0.0` to opt out and preserve
the legacy "kept user default" behavior on cold start. New helpers
`claude_hpc.infra.clusters.get_cold_start_mem_buffer` and
`get_nfs_data_dir` parse and validate the new fields. Both new keys
are added to the boundary-contract allowlist as infra-shaped (they
describe how the cluster is configured, not what work the user wants
to run).

### Changed (deprecation) — `hpc_mapreduce` → `claude_hpc` package rename

The package import path has been renamed `hpc_mapreduce` → `claude_hpc`,
matching the distribution name in `pyproject.toml`. The package was
also split into 4 sub-packages reflecting their domains:

- `claude_hpc.mapreduce` — the actual mapreduce tool (dispatch, combine, reduce, templates)
- `claude_hpc.infra` — cluster communications (backends, ssh, inspect)
- `claude_hpc.orchestrator` — job submission orchestration (flow primitives, planner, runs, runtime priors)
- `claude_hpc.forecast` — predictive scheduling (queue-wait baseline, DES simulator, microstructure features)
- `claude_hpc._internal` — shared utilities (_io, _time, _version, _primitive, idempotency, layout, lifecycle, telemetry)
- `claude_hpc.atoms` — CLI-only primitive dispatchers

`hpc_mapreduce` continues to work as a deprecation shim for one release
— it emits a `DeprecationWarning` on import and forwards `*` from
`claude_hpc`. Update your imports to `claude_hpc` directly; the shim
will be removed in a future release.

The user-facing CLI binary `hpc-mapreduce` is unchanged. Slash commands,
JSON envelope contracts, the `.hpc/tasks.py` user contract, JSON Schema
shapes (now under `claude_hpc/schemas/`), and the cluster-side
stdlib-only constraint on `dispatch.py` and `combiner.py` are all
preserved exactly.

The `cmd_capabilities` output's `python` field now reflects the new
module paths (e.g. `claude_hpc.orchestrator.submit_flow.submit_flow`
instead of `hpc_mapreduce.job.submit_flow.submit_flow`); agents that
shell out by `cli` are unaffected.

### Removed (breaking) — SEGV blacklist feature

The SEGV blacklist (`claude_hpc.orchestrator.blacklist`, the
`record-segv-blacklist` primitive, the `record_segv` /
`get_active_blacklist` exports) has been removed. The smart planner no
longer consumes a blacklist signal; callers should drop any reference to
the feature.

- **`schemas/plan_submit.output.json`**: the top-level required field
  `blacklist_active_count` and the per-candidate optional property
  `blacklisted_nodes` are removed. Pinned consumers should re-pin against
  the current schema.
- **Public API**: `record_segv`, `get_active_blacklist` removed from
  `hpc_mapreduce.__all__`.
- **Docs**: `docs/primitives/record-segv-blacklist.md` deleted;
  `docs/generated/operations.md` and `docs/primitives/README.md` regenerated.

### Added — `hpc_mapreduce.layout` and `hpc_mapreduce.lifecycle` (B1, B2)

- **B1** `hpc_mapreduce.layout` introduces `RepoLayout(experiment_dir)`
  and `JournalLayout(experiment_dir)` — frozen dataclasses that replace
  eight scattered path helpers (`framework_subdir`, `runs_subdir`,
  `tasks_path`, `run_sidecar_path`, `_runs_dir`, `blacklist_path`,
  `runtime_path`, `journal_dir` / `runs_dir` / `_run_path`). The two
  classes are *types*, so the pre-B1 `runs_dir` (journal) vs
  `runs_subdir` (cluster sidecar) name collision — which caused the
  recent `wave_map` P0 — is now a static type error rather than a
  prose-only convention. The eight helpers are kept as deprecated
  forwarders for back-compat.
- **B2** `hpc_mapreduce.lifecycle` introduces four `StrEnum`
  vocabularies replacing the four scattered, drifting string sets that
  preceded them: `JournalStatus` (RunRecord status),
  `LifecycleState` (workflow envelope state), `TaskStatus` (per-task
  status), `FailureCategory` (failure-fingerprint vocabulary).
  `tests/test_lifecycle.py` cross-validates that the schema enums on
  `monitor_flow.output.json`, `status.output.json`, and
  `reconcile.output.json` match `LifecycleState`, that classifier
  emissions are a subset of `FailureCategory`, and — pinning the A4
  invariant — that classifier emissions are a subset of the resubmit
  path's accepted set. The drift class is now unrepresentable.

### Fixed — cross-cutting audit (A1–A11)

- **A1** `docs/primitives/check-preflight.md` frontmatter pointed
  `backed_by.python` at a non-existent module
  (`hpc_mapreduce.preflight.run`); routed to the canonical
  `hpc_mapreduce.agent_cli.cmd_preflight`.
- **A2** Four primitive docs claimed bogus per-experiment mirror writes
  that no implementation actually performed — `submit-spec.md`,
  `mark-run-terminal.md`, and `resubmit-failed.md` had stale "writes
  `<experiment_dir>/.hpc/runs/...` (mirror)" lines; `monitor-flow.md`
  declared the wrong path for `.monitor.jsonl` (the real path is in the
  journal dir, not the per-experiment dir). Frontmatter now matches
  reality.
- **A3** `hpc_mapreduce/schemas/status.output.json` and
  `reconcile.output.json` enums missed `timeout`, which
  `monitor_flow.output.json` already accepts. Consumers reading status
  output must accept any value the workflow primitive could plausibly
  produce; both schemas now include it.
- **A4** Failure-category vocabulary divergence: the auto-classifier in
  `slash_commands.runner.cluster_failures_by_fingerprint` emitted 5
  categories (`import_error`, `file_not_found`, `permission_denied`,
  `disk_full`, `python_traceback`) that the resubmit subcommand silently
  rejected. Took the union as canonical; added a regression test pinning
  the invariant.
- **A5** `slash_commands.runner.submit_and_record` now accepts an
  optional `cmd_sha` and dedups against `find_run_by_cmd_sha` when the
  journal is empty but the per-experiment sidecar still exists. Repairs
  the journal in-place so subsequent calls hit `load_run` directly.
- **A6** Authored four missing primitive docs:
  `docs/primitives/walltime-drift.md`, `house-edge.md`, `logs.md`,
  `failures.md`. Each follows the existing frontmatter contract and
  parses cleanly through `tests/test_primitive_frontmatter.py`.
- **A7** Replaced the hand-typed `subcommands` literal in
  `cmd_capabilities` with a derivation from the live argparse tree.
  `walltime-drift` and `house-edge` now appear automatically in
  capabilities output.
- **A8** Corrected the `hpc_mapreduce/agent_cli.py` module docstring,
  which falsely claimed stderr is JSON-per-line. Stderr is free-form
  diagnostic prose (`[dispatch] ERROR: ...`); only stdout carries
  envelopes.
- **A9** `_append_tick` in `hpc_mapreduce/job/monitor_flow.py` now
  acquires a flock on a sibling `.lock` file before writing the JSONL
  record. The slash-command surface and the workflow primitive both
  append to the same `<run_id>.monitor.jsonl`; without flock, two
  concurrent writers could interleave bytes mid-line. Best-effort no-op
  on platforms without `fcntl`.
- **A10** `read_run_sidecar` now warns once per
  `(run_id, sidecar_version)` when the sidecar's `claude_hpc_version`
  differs from the running package's `__version__`. Closes the loop on
  a previously-dead sidecar field; readers can find old sidecars in the
  wild.
- **A11** `hpc_mapreduce.infra.inspect.inspect_cluster` raised a bare
  `KeyError` for unknown clusters, which the envelope translator
  surfaced as `error_code: internal`. Replaced with
  `errors.ClusterUnknown` so the typed exception flows through
  `_err_from_hpc` to produce the documented `error_code: cluster_unknown`.

### Removed — `claude_hpc.orchestrator.campaign.run_campaign` asyncio loop and `defaults` callbacks

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
- `claude_hpc.mapreduce.reduce.history.prior(...)` for reading per-iteration
  reduced metrics back inside `tasks.py`.
- `claude_hpc.orchestrator.campaign.campaign_dir(...)` for strategy-state
  placement (Optuna SQLite, PBT checkpoints).
- `hpc-mapreduce campaign list / status` CLI inspection.

For the migration story (every capability the asyncio loop offered has
an equivalent in the slash-command pattern, including K-in-flight,
FIRST_COMPLETED-style waits via parallel `Bash` calls, wall-clock
budget caps via env var + `tasks.py`, and headless overnight runs via
`/loop`), see `docs/workflows/campaign.md` and `slash_commands/commands/campaign-hpc.md`.

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
- **`claude_hpc.orchestrator.blacklist`** — append-only SEGV journal at
  `<repo>/.hpc/bad_nodes.<cluster>.json`. 7-day TTL, refreshed on
  repeat SEGVs. Atomic write under `fcntl.flock`. Evidence list capped
  at 5 most-recent entries per node. `record_segv()` is called by
  `/hpc-monitor` on `NODE_FAIL` / `exit -11`; `get_active()` is called
  by the planner with TTL filtering.
- **`claude_hpc.forecast.runtime_prior`** — append-only sample log at
  `<repo>/.hpc/runtimes/<profile>.<cluster>.json`. `roll_up_quantiles()`
  groups by `gpu_type` and computes p50 / p95 / p99 / mean / n_samples,
  with optional `cmd_sha` filter so a `.hpc/tasks.py` change can
  invalidate stale priors.
- **`claude_hpc.orchestrator.planner`** — `plan-submit --profile <p>
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
end-to-end Optuna recipe in `docs/workflows/campaign.md`. None bind the framework
to a specific tuning library; they collapse boilerplate the previous
shape made every user write themselves.

- **`claude_hpc.orchestrator.campaign.campaign_dir(experiment_dir, campaign_id)`** —
  canonical scratch directory `.hpc/campaigns/<cid>/`. Created
  idempotently. Reserved for strategy libraries to put state files
  (Optuna SQLite, PBT checkpoints, walk-forward cursor); the framework
  writes nothing inside.
- **`claude_hpc.orchestrator.campaign.defaults`** — three curried-function defaults
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
- **`claude_hpc.mapreduce.metrics_io.read_kw_env()`** — executor-side helper
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
`claude_hpc.mapreduce.reduce.history.prior(experiment_dir, campaign_id)` at
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
- **`claude_hpc.mapreduce.reduce.history`** — read-only accessor:
  - `prior(experiment_dir, campaign_id)` returns per-iteration reduced
    metric dicts, oldest-first. Pending iterations contribute `{}`.
  - `find_sidecars_by_campaign` and `result_dirs_for_sidecar` for
    callers that need the underlying primitives. None of these import
    `.hpc/tasks.py` (the loop's calling module), so no recursion.
- **`claude_hpc.orchestrator.campaign.run_campaign`** — asyncio in-flight queue.
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
  `docs/workflows/campaign.md` (random search, Optuna ask/tell, walk-forward).

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
  helper in `claude_hpc.mapreduce.reduce.status`.
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
  - `docs/workflows/mars-integration.md` — Bun.spawn env block, `error_code` →
    retry-policy mapping, troubleshooting flow for the silent-hang
    failure mode, journal-coexistence rules.
  - `docs/workflows/mars/experiment-runner.snippet.md` — paste-ready section for
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
  `docs/workflows/mars-integration.md`; kept the SSH-passthrough warning visible.

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
  in `docs/reference/cli-spec.md`; runtime-validatable JSON Schemas under
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
- **New docs**: `docs/reference/cli-spec.md`, `docs/reference/config-precedence.md`,
  `docs/internals/sync-checklist.md`.

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
