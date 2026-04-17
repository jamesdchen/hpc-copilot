# CLI Contract

Authoritative reference for every CLI and helper that Claude invokes from the
slash-commands. Keep responses deterministic, JSON-shaped, and grep-friendly.

Conventions
-----------
- Stdout is a single JSON object (no logs, no banners). Errors go to stderr.
- Every top-level schema below is stable across manifest `schema_version` 2+.
- Structured return shapes use `{<data_key>, errors}` where `errors` is a list
  of `{code: str, detail: str}` objects (empty list means success).

Manifest filenames
------------------
Dispatch manifests are written with a content-addressed filename inside the
experiment directory:

- Canonical form: `manifest.<cmd_sha_short>.json` where `cmd_sha_short` is
  the first 8 chars of the run-level `cmd_sha`. The run-level `cmd_sha` is
  computed by `hpc_mapreduce.job.manifest.aggregate_cmd_sha` as
  `SHA-256(join("\n", sorted per-task cmd_sha values))`.
- Alias: `manifest.json` is kept in sync with the most recent
  content-addressed manifest (symlink where supported, copy-fallback
  otherwise). Tools that previously opened `manifest.json` continue to
  work unchanged.
- Retention: at most `hpc_mapreduce.job.manifest.MAX_MANIFESTS` (default 10)
  content-addressed manifests are kept per experiment directory. Oldest by
  mtime are evicted on every write.
- The manifest *contents* are unchanged — `schema_version`, `total_tasks`,
  `tasks.<tid>.cmd`, `tasks.<tid>.cmd_sha`, etc. still match the existing
  shape. Only the on-disk filename convention is additive.

When resuming a prior run, `/submit` picks up an existing
`manifest.<cmd_sha_short>.json` and delegates to
`hpc_mapreduce.job.resubmit.resubmit_plan` for the failing task IDs; see
`commands/submit.md` for the interactive resume-vs-fresh prompt.

---

## `python -m hpc_mapreduce.reduce.status`

Emit a full status report for a dispatch manifest.

| Arg | Required | Description |
|---|---|---|
| `--manifest` | yes | Path to `_hpc_dispatch.json` |
| `--job-ids` | no | Comma-separated scheduler job IDs |
| `--job-name` | no | Job name (for error-log lookup) |
| `--scheduler` | no | `sge` or `slurm` (auto-detected if omitted) |
| `--file-glob` | no | Glob for per-task result files (default `*`) |
| `--log-dir` | no | SLURM log directory |
| `--scratch-dir` | no | SGE scratch log directory |
| `--slurm-cluster` | no | `sacct --clusters` value |
| `--sge-user` | no | `qstat -u` value |
| `--min-rows` | no | CSV min-row threshold (default 0) |

**Stdout JSON schema** — all four top-level keys always present:

```json
{
  "summary": {"complete": 0, "running": 0, "pending": 0, "failed": 0, "unknown": 0},
  "tasks":   {"<tid>": {"status": "complete|running|pending|failed|unknown",
                        "cmd_sha": "<16-hex>|null", "...": "..."}},
  "rollup":  {"<grid_point_key>": {"complete": 0, "running": 0, "pending": 0,
                                   "failed": 0, "unknown": 0, "total": 0}},
  "errors":  [{"code": "...", "detail": "..."}]
}
```

- `tasks[tid].cmd_sha` echoes the manifest v2 per-task `cmd_sha` (first 16 hex
  chars of SHA-256 of the task's `cmd`) so observers can detect drift.
- Exit code: `0` on success, `2` if the manifest is missing or unparseable.

## `python3 _hpc_dispatch.py` (on cluster)

Runs one task. Reads `TASK_ID` from env, looks it up in `_hpc_dispatch.json`,
execs the resolved command.

| Env | Required | Description |
|---|---|---|
| `TASK_ID` | yes | 1-based task index (derived from `SGE_TASK_ID`/`SLURM_ARRAY_TASK_ID`) |
| `HPC_MANIFEST` | no | Manifest path (default `_hpc_dispatch.json`) |

Exit codes: `0` on task success, non-zero on setup or dispatch failure.

Stderr messages that `/monitor` greps for:
- `TASK_ID unset`
- `manifest missing`
- `schema_version unsupported`

## `python3 _hpc_combiner.py` (on cluster)

Aggregates completed tasks belonging to one wave into `_combiner/wave_N.json`.

| Arg | Required | Description |
|---|---|---|
| `--wave` | yes | Wave index `N` (int) |
| `--manifest` | no | Manifest path (default `_hpc_dispatch.json`) |
| `--force` | no | Re-run even if `_combiner/wave_N.json` already exists |

Env fallbacks when args are absent: `HPC_WAVE`, `HPC_MANIFEST`.

**Output file** `_combiner/wave_N.json`:

```json
{
  "wave": 0,
  "task_ids": ["1", "2", "..."],
  "grid_points": {"<grid_point_key>": {"...": "..."}},
  "errors": [{"code": "...", "detail": "..."}]
}
```

Exit `0` on success (wave combined), non-zero on failure (e.g. missing task
results, reducer raised). Stderr carries a short excerpt `/monitor` can show.

## `hpc_mapreduce.infra.backends.query.query_sacct`, `query_sge`

Unified return shape:

```python
{
    "tasks": {tid: {"state": "...", "...": "..."}},
    "errors": [{"code": "...", "detail": "..."}],
}
```

`tid` is 1-based `int`. Callers should treat a non-empty `errors` list as a
partial-result signal, not as a hard failure.

## `hpc_mapreduce.infra.gpu.pick_gpu`

```python
{
    "gpus": [{"index": 0, "free_mem_mb": 40000, "...": "..."}],
    "errors": [{"code": "...", "detail": "..."}],
}
```

## `hpc_mapreduce.infra.remote.run_combiner_checked`

```python
def run_combiner_checked(
    ssh_target: str,
    remote_path: str,
    wave: int,
    manifest: str = "_hpc_dispatch.json",
    force: bool = False,
) -> tuple[bool, str, str]:
    """Run the on-cluster combiner and return (ok, stdout, stderr)."""
```

- `ok` is `True` iff the underlying SSH process exits 0.
- `stdout` is the combiner's JSON (when produced) or empty.
- `stderr` should be captured into the `/monitor` state blob on failure so the
  user can see why a wave did not combine.
