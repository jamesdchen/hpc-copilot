# Surface Sync Checklist

Both surfaces share the atomic-ops layer (`slash_commands/runner.py`)
for any mutating op. Anything below MUST stay aligned across the two
surfaces; changing it is a breaking change requiring a version bump.

The list is exhaustive for the v0.2.0 contract. When you add a new
shared invariant, add it here in the same PR; when you change one,
update both surfaces and bump the version.

## Shared invariants

### `run_id` format

- **Recommended format**: `f"{profile}-{utc_ts}-{cmd_sha[:8]}"` where
  `utc_ts` is `YYYYMMDD-HHMMSS` and `cmd_sha` is computed by
  `claude_hpc.orchestrator.runs.compute_cmd_sha(tasks_module)` over the
  materialized `[tasks.resolve(i) for i in range(tasks.total())]`.
- **Validation**: `claude_hpc.orchestrator.runs.run_sidecar_path` accepts any
  string matching `[A-Za-z0-9._\-]+`; the recommended format keeps
  sidecars sorted chronologically by mtime ↔ filename.
- **Defined in**: `slash_commands/runner.py:submit_and_record` —
  `run_id` is a required keyword.
- **Public contract**: MARs and orchestrator agents may key state on
  this. Renaming the format breaks every downstream consumer.

### `error_code` enum

The full set of 12 values that may appear in an error envelope's
`error_code` field. Defined as `HpcError` subclasses in
`slash_commands/errors.py`.

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
| `internal` | `HpcError` (base / catch-all) | internal | no |

The same enum appears in `hpc_mapreduce/schemas/envelope.json`. Adding
a value requires updating both files.

### `failure_category` enum

Values returned by `claude_hpc.mapreduce.reduce.classify.classify_failure`.
Used by `/monitor-hpc` (slash) and any agent that wants to drive auto-retry
policy. The complete list (`CATEGORIES` constant in
`hpc_mapreduce/reduce/classify.py`):

- `gpu_oom`
- `system_oom`
- `walltime`
- `node_failure`
- `queue_stall`
- `code_bug`
- `unknown`

Order is significant in the classifier (first-match-wins, specific
patterns before generic traceback). The `--spec.category` field of
`hpc-mapreduce resubmit` is documented to accept these values; see the
discrepancy note below.

### `lifecycle_state` enum

Possible values of `RunRecord.status`:

- `in_flight` — submitted, monitoring active. The default.
- `complete` — terminal, all tasks succeeded and any combiner waves
  finished.
- `failed` — terminal, run aborted with unrecoverable failure.
- `abandoned` — terminal, no `job_ids` are alive on the scheduler
  (set by `runner.reconcile`).

Defined in `slash_commands/session.py` (`TERMINAL_STATUSES` frozenset
+ default `status="in_flight"` on `RunRecord`). Validated in
`mark_run`.

### Journal `schema_version`

- **Current value**: `1` (the constant `SCHEMA_VERSION` in
  `slash_commands/session.py`).
- Records with a mismatched `schema_version` are skipped (warned, not
  raised) by `load_run`. Bumping requires a migration story.

### `clusters.yaml` schema

- **Shipped at**: `hpc_mapreduce/config/clusters.yaml`.
- **Loader**: `hpc_mapreduce.load_clusters_config` (re-exported from
  `hpc_mapreduce.infra.clusters`).
- **Schema**: documented in `docs/boundary-contract.md` under "Config
  split". Allowed keys enforced by
  `tests/test_boundary_contract.py:test_clusters_yaml_is_infra_only`.

### Per-run sidecar v2 schema

- **Lives in**: `<experiment>/.hpc/runs/<run_id>.json`.
- **Writer**: `claude_hpc.orchestrator.runs.write_run_sidecar`.
- **Reader**: `claude_hpc.orchestrator.runs.read_run_sidecar` (backfills v1
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
- **JSON Schema**: `hpc_mapreduce/schemas/stages.input.json`.
- **Loader**: `claude_hpc.orchestrator.stages.load_stages` (validates against
  the schema and enforces unique names + resolved `depends_on`).

### Exit-code → error_code mapping

- **Documented in**: `docs/cli-spec.md` ("Exit code → error_code
  mapping" section).
- **Source of truth**: `_EXIT_CODE_BY_CATEGORY` in `hpc_mapreduce/agent_cli.py`.

### Last-status cache file

- **Path**: `<HPC_JOURNAL_DIR>/<repo_hash>/runs/<run_id>.last_status.json`.
- **Writer**: `slash_commands/runner.py:record_status` (best-effort;
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
- **Helpers**: `claude_hpc.orchestrator.runs.{write,read}_run_sidecar`,
  `find_existing_runs`, `find_run_by_cmd_sha`, `prune_old_runs`,
  `compute_cmd_sha`, `run_sidecar_path`. All re-exported at package
  root; see `docs/boundary-contract.md`.
- **Retention**: `MAX_RUNS = 10`, oldest by mtime evicted on every
  write.
- **Identity**: the `run_id` string is the sole identifier; sidecars
  are addressable directly at `.hpc/runs/<run_id>.json`.

## How to extend

When you add a new invariant or change one of the above:

1. Update the source-of-truth file (the Python module that defines it).
2. Update this checklist with the new value/format and pointer to the
   defining file.
3. Update the relevant downstream doc (`cli-spec.md`,
   `boundary-contract.md`, `schema.md`, `config-precedence.md`).
4. Update the JSON Schema under `hpc_mapreduce/schemas/` if the
   invariant is part of the CLI envelope contract.
5. Bump the package version in `pyproject.toml`.

## Known discrepancies (v0.2.0)

None at release. The `--spec.category` enum in
`hpc_mapreduce/schemas/resubmit.input.json` is the canonical mirror of
`CATEGORIES` in `hpc_mapreduce/reduce/classify.py`; if you add a new
failure category, update both files in the same commit.
