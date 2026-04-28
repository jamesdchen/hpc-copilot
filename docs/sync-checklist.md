# Surface Sync Checklist

Both surfaces share the atomic-ops layer (`slash_commands/runner.py`)
for any mutating op. Anything below MUST stay aligned across the two
surfaces; changing it is a breaking change requiring a version bump.

The list is exhaustive for the v0.2.0 contract. When you add a new
shared invariant, add it here in the same PR; when you change one,
update both surfaces and bump the version.

## Shared invariants

### `run_id` format

- **Format**: `f"{profile}_{cmd_sha8}"` where `cmd_sha8` is the first
  8 hex chars of `sha256(canonicalized_manifest)` (extracted from the
  manifest filename `manifest.<sha8>.json`).
- **Defined in**: `slash_commands/runner.py:submit_and_record` (lines
  74–76 — `manifest_filename.removeprefix("manifest.").removesuffix(".json")`).
- **Public contract**: MARs and orchestrator agents may key state on
  this. Renaming the format breaks every downstream consumer.

### `error_code` enum

The full set of 9 values that may appear in an error envelope's
`error_code` field. Defined as `HpcError` subclasses in
`slash_commands/errors.py`.

| `error_code` | Class | `category` | `retry_safe` |
|---|---|---|---|
| `ssh_unreachable` | `SshUnreachable` | network | yes |
| `scheduler_throttled` | `SchedulerThrottled` | cluster | yes |
| `manifest_invalid` | `ManifestInvalid` | user | no |
| `executor_not_found` | `ExecutorNotFound` | user | no |
| `cluster_unknown` | `ClusterUnknown` | user | no |
| `journal_corrupt` | `JournalCorrupt` | internal | no |
| `remote_command_failed` | `RemoteCommandFailed` | cluster | no |
| `config_invalid` | `ConfigInvalid` | user | no |
| `internal` | `HpcError` (base / catch-all) | internal | no |

The same enum appears in `hpc_mapreduce/schemas/envelope.json`. Adding
a value requires updating both files.

### `failure_category` enum

Values returned by `hpc_mapreduce.reduce.classify.classify_failure`.
Used by `/status` (slash) and any agent that wants to drive auto-retry
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

### `hpc.yaml` schema

- **Lives in**: the experiment repo (optional file).
- **Schema**: documented in `docs/schema.md` (top-level fields,
  profiles, single- vs multi-stage, grid, env, resources, results,
  backtest, constraints, cluster_envs).

### Exit-code → error_code mapping

- **Documented in**: `docs/cli-spec.md` ("Exit code → error_code
  mapping" section).
- **Source of truth**: `_EXIT_CODE_BY_CATEGORY` in `hpc_mapreduce/cli.py`.

### Last-status cache file

- **Path**: `<HPC_JOURNAL_DIR>/<repo_hash>/runs/<run_id>.last_status.json`.
- **Writer**: `slash_commands/runner.py:record_status` (best-effort;
  a write failure does not roll back the journal update).
- **Reader**: any consumer — agent, human, `jq` pipeline, file
  watcher. Mtime tells the caller how stale the snapshot is.
- **Shape**: same as `RunRecord.last_status` plus a `checked_at`
  ISO-8601 UTC timestamp. Stable across `schema_version` 1.

### Manifest filename convention

- **Format**: `manifest.<cmd_sha8>.json` (8 lowercase hex chars).
- **Validated in**: `hpc_mapreduce/schemas/submit.input.json`
  (`pattern: ^manifest\.[0-9a-f]{8}\.json$`) and
  `runner.submit_and_record` (which strips the prefix/suffix to derive
  `run_id`).
- **Alias**: `manifest.json` symlinks/copies to the active manifest;
  see `docs/cli-contract.md` (Python-API contract).

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

- The `--spec.category` enum in
  `hpc_mapreduce/schemas/resubmit.input.json` lists
  `["oom", "gpu_oom", "walltime", "code_bug", "infra", "unknown"]`,
  which **does not match** the `CATEGORIES` tuple in
  `hpc_mapreduce/reduce/classify.py`
  (`gpu_oom, system_oom, walltime, node_failure, queue_stall,
  code_bug, unknown`). The schema accepts `oom` and `infra` (neither
  is a classifier output) and rejects `system_oom`, `node_failure`,
  `queue_stall` (all valid classifier outputs). One of the two files
  needs to win; resolve before the next minor version bump.
