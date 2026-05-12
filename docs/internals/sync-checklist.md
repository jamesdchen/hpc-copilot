# Surface Sync Checklist

Both surfaces share the atomic-ops layer (`claude_hpc/runner/`)
for any mutating op. Anything below MUST stay aligned across the two
surfaces; changing it is a breaking change requiring a version bump.

The list is exhaustive for the v0.2.0 contract. When you add a new
shared invariant, add it here in the same PR; when you change one,
update both surfaces and bump the version.

## Shared invariants

### `run_id` format

- **Recommended format**: `f"{profile}-{utc_ts}-{cmd_sha[:8]}"` where
  `utc_ts` is `YYYYMMDD-HHMMSS` and `cmd_sha` is computed by
  `claude_hpc.state.runs.compute_cmd_sha(tasks_module)` over the
  materialized `[tasks.resolve(i) for i in range(tasks.total())]`.
- **Validation**: `claude_hpc.state.runs.run_sidecar_path` accepts any
  string matching `[A-Za-z0-9._\-]+`; the recommended format keeps
  sidecars sorted chronologically by mtime ↔ filename.
- **Defined in**: `claude_hpc/runner/:submit_and_record` —
  `run_id` is a required keyword.
- **Public contract**: MARs and orchestrator agents may key state on
  this. Renaming the format breaks every downstream consumer.

### `error_code` enum

The full set of 15 values that may appear in an error envelope's
`error_code` field. Defined as `HpcError` subclasses in
`claude_hpc/errors.py`.

| `error_code` | Class | `category` | `retry_safe` |
|---|---|---|---|
| `ssh_unreachable` | `SshUnreachable` | network | yes |
| `scheduler_throttled` | `SchedulerThrottled` | cluster | yes |
| `spec_invalid` | `SpecInvalid` | user | no |
| `executor_not_found` | `ExecutorNotFound` | user | no |
| `cluster_unknown` | `ClusterUnknown` | user | no |
| `journal_corrupt` | `JournalCorrupt` | internal | no |
| `remote_command_failed` | `RemoteCommandFailed` | cluster | no |
| `config_invalid` | `ConfigInvalid` | user | no |
| `combiner_failed` | `CombinerFailed` | cluster | yes |
| `cluster_timeout` | `ClusterTimeout` | cluster | yes |
| `outputs_missing` | `OutputsMissing` | cluster | yes |
| `cluster_partially_degraded` | `ClusterPartiallyDegraded` | cluster | yes |
| `preempted` | `Preempted` | cluster | yes |
| `schema_incompat` | `SchemaIncompat` | internal | no |
| `internal` | `HpcError` (base / catch-all) | internal | no |

The same enum appears in `claude_hpc/schemas/envelope.json` —
generated from `_schema_models/_shared.py:ErrorCode` so adding
a value is a one-place edit (Python alias) followed by
`scripts/build_schemas.py --write`.

### `failure_category` enum

Values returned by `claude_hpc.mapreduce.reduce.classify.classify_failure`.
Used by `/monitor-hpc` (slash) and any agent that wants to drive auto-retry
policy. The complete list (`CATEGORIES` constant in
`claude_hpc/mapreduce/reduce/classify.py`):

- `gpu_oom`
- `system_oom`
- `walltime`
- `node_failure`
- `queue_stall`
- `code_bug`
- `unknown`

Order is significant in the classifier (first-match-wins, specific
patterns before generic traceback). The `--spec.category` field of
`hpc-agent resubmit` is documented to accept these values; see the
discrepancy note below.

### `lifecycle_state` enum

Possible values of `RunRecord.status`:

- `in_flight` — submitted, monitoring active. The default.
- `complete` — terminal, all tasks succeeded and any combiner waves
  finished.
- `failed` — terminal, run aborted with unrecoverable failure.
- `abandoned` — terminal, no `job_ids` are alive on the scheduler
  (set by `runner.reconcile`).

Defined in `claude_hpc/_internal/session/` (`TERMINAL_STATUSES`
frozenset lives in `run_record.py`; default `status="in_flight"` is
set on `RunRecord` there too). Validated in `mark_run` (in
`journal.py`).

### Journal `schema_version`

- **Current value**: `1` (the constant `SCHEMA_VERSION` in
  `claude_hpc/_internal/session.py`).
- Records with a mismatched `schema_version` are skipped (warned, not
  raised) by `load_run`. Bumping requires a migration story.

### `clusters.yaml` schema

- **Shipped at**: `claude_hpc/config/clusters.yaml`.
- **Loader**: `claude_hpc.load_clusters_config` (re-exported from
  `claude_hpc.infra.clusters`).
- **Schema**: documented in `docs/reference/boundary-contract.md` under "Config
  split". Allowed keys enforced by
  `tests/test_boundary_contract.py:test_clusters_yaml_is_infra_only`.

### Per-run sidecar v2 schema

- **Lives in**: `<experiment>/.hpc/runs/<run_id>.json`.
- **Writer**: `claude_hpc.state.runs.write_run_sidecar`.
- **Reader**: `claude_hpc.state.runs.read_run_sidecar` (backfills v1
  records with v2 keys defaulted to None).
- **Fields**: identity (`run_id`, `cmd_sha`, `tasks_py_sha`,
  `submitted_at`, `claude_hpc_version`), executor (`executor`,
  `result_dir_template`, `task_count`), wave map, plus the v2
  config-snapshot block (`cluster`, `profile`, `campaign_id`, `project`,
  `remote_path`, `resources`, `env`, `env_group`, `constraints`,
  `gpu_fallback`, `max_retries`, `runtime`, `auto_retry`,
  `aggregate_defaults`).

### Multi-stage DAG schema

- **Lives in**: `<experiment>/.hpc/stages.py` (Python file exposing
  `def stages() -> list[dict]`).
- **JSON Schema**: `claude_hpc/schemas/stages.input.json`.
- **Loader**: `claude_hpc.planning.stages.load_stages` (validates against
  the schema and enforces unique names + resolved `depends_on`).

### Exit-code → error_code mapping

- **Documented in**: `docs/reference/cli-spec.md` ("Exit code → error_code
  mapping" section).
- **Source of truth**: `_EXIT_CODE_BY_CATEGORY` in `claude_hpc/agent_cli.py`.

### Last-status cache file

- **Path**: `<HPC_JOURNAL_DIR>/<repo_hash>/runs/<run_id>.last_status.json`.
- **Writer**: `claude_hpc/runner/:record_status` (best-effort;
  a write failure does not roll back the journal update).
- **Reader**: any consumer — agent, human, `jq` pipeline, file
  watcher. Mtime tells the caller how stale the snapshot is.
- **Shape**: same as `RunRecord.last_status` plus a `checked_at`
  ISO-8601 UTC timestamp. Stable across `schema_version` 1.

### Per-run sidecar layout

- **Path**: `.hpc/runs/<run_id>.json` inside the experiment repo.
- **Schema**: `sidecar_schema_version` (currently `1`), `run_id`,
  `cmd_sha`, `claude_hpc_version`, `submitted_at`, `executor`,
  `result_dir_template`, `task_count`, `tasks_py_sha`, optional
  `wave_map` and `extra` pocket.
- **Helpers**: `claude_hpc.state.runs.{write,read}_run_sidecar`,
  `find_existing_runs`, `find_run_by_cmd_sha`, `prune_old_runs`,
  `compute_cmd_sha`, `run_sidecar_path`. All re-exported at package
  root; see `docs/reference/boundary-contract.md`.
- **Retention**: `MAX_RUNS = 10`, oldest by mtime evicted on every
  write.
- **Identity**: the `run_id` string is the sole identifier; sidecars
  are addressable directly at `.hpc/runs/<run_id>.json`.

## Where source-of-truth lives

The migration to Pydantic-as-authoring-SoT means most cross-cutting
invariants now have a single Python definition; the JSON schemas are
regenerated, the markdown is regenerated, and the cross-file `$ref`
graph that used to hold them together is gone.

| Invariant | Python SoT | Generated artifacts |
|---|---|---|
| `error_code` enum | `_schema_models/_shared.py:ErrorCode` + `errors.py` HpcError subclasses | `schemas/envelope.json`, every Pydantic model that types `error_code` |
| `failure_category` enum | `mapreduce/reduce/classify.py:CATEGORIES` (still hand-mirrored — see below) | `schemas/resubmit.input.json` (Pydantic alias `ResubmitCategory`) |
| Lifecycle states | `_internal/session.py:TERMINAL_STATUSES` (Python frozenset) + `_schema_models/_shared.py:LifecycleState{Terminal,Observable,…}` (Pydantic Literal) | every Pydantic model that types lifecycle |
| `run_id` shape | `_schema_models/_shared.py:RunIdStrict` (input), `RunIdLoose` (output) | every input/output schema that types a run_id |
| Scheduler / GpuType / Runtime / BackendName | `_schema_models/_shared.py` aliases | every consumer model |
| `@primitive` decorator metadata (name, verb, side_effects, idempotent, idempotency_key, error_codes, composes, cli, agent_facing, exit_codes) | `_internal/_primitive.py` registry | `docs/primitives/<name>.md` frontmatter, `docs/primitives/README.md` table, `docs/generated/operations.md` |
| Wire envelope shape | `_schema_models/envelope.py:EnvelopeAdapter` | `schemas/envelope.json` |

## How to extend

When you add a new invariant or change one of the above:

1. Edit the Python SoT (the table above tells you which file).
2. Run the regen scripts (or `pre-commit run -a`):
   - `scripts/build_schemas.py --write` regenerates JSON schemas.
   - `scripts/build_primitive_frontmatter.py --write` regenerates
     `docs/primitives/<name>.md` frontmatter.
   - `scripts/build_primitive_index.py` regenerates the catalog
     table.
   - `scripts/build_operations_index.py` regenerates
     `docs/generated/operations.md`.
3. Update prose docs that explain WHY the invariant exists if the
   semantic changed (`cli-spec.md`, `boundary-contract.md`,
   `config-precedence.md`).
4. Bump the package version in `pyproject.toml` for breaking
   wire-contract changes.

## Known discrepancies (v0.2.0)

`CATEGORIES` in `claude_hpc/mapreduce/reduce/classify.py` is still
the hand-authored Python source for failure categories; the
`ResubmitCategory` Literal in
`_schema_models/resubmit.py` mirrors it manually. Adding a new
failure category requires updating both. Future cleanup: lift
`CATEGORIES` into `_schema_models/_shared.py` and re-export from
`classify.py` so there's one definition.
