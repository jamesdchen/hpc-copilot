# Python-API Contract

Authoritative reference for every Python helper, on-cluster CLI, and
process-level entry point that the slash commands and library callers
invoke from inside the `claude-hpc` checkout. This is the **Python-API
surface**: function signatures, on-cluster `python -m ...` invocations,
and the JSON shapes those produce. Keep responses deterministic,
JSON-shaped, and grep-friendly.

> **Looking for the shell `hpc-mapreduce` CLI?** That is the
> agent-facing surface — see [`docs/cli-spec.md`](cli-spec.md). This
> document covers the Python/library and on-cluster paths that
> `slash_commands/*.py` and the CLI both reach into.

Conventions
-----------
- Stdout is a single JSON object (no logs, no banners). Errors go to stderr.
- Every top-level schema below is stable across `sidecar_schema_version` 1+.
- Structured return shapes use `{<data_key>, errors}` where `errors` is a list
  of `{code: str, detail: str}` objects (empty list means success).

Run identity (`.hpc/runs/<run_id>.json`)
-----------------------------------------
Each `/submit` writes a per-run sidecar to `.hpc/runs/<run_id>.json`:

```json
{
  "sidecar_schema_version": 2,
  "run_id": "ml_ridge-20260429-153012-abc12345",
  "cmd_sha": "...",
  "claude_hpc_version": "0.5.0",
  "submitted_at": "2026-04-29T15:30:12Z",
  "executor": "python3 src/ml_ridge.py",
  "result_dir_template": "results/{model}_{seed}",
  "task_count": 24,
  "tasks_py_sha": "...",
  "wave_map": {"0": [0, 1, ...]},

  "cluster": "hoffman2",
  "profile": "ml_ridge",
  "campaign_id": "ml_ridge_optuna_q1",
  "project": "ml-ridge",
  "remote_path": "/u/scratch/u/me/ml_ridge",
  "resources": {"cpus": 4, "mem": "16G", "walltime": "02:00:00"},
  "env": {"modules": "python/3.11.9", "conda_env": "ml"},
  "env_group": "default",
  "constraints": {"max_array_size": 500},
  "gpu_fallback": ["a100", "h100"],
  "max_retries": 3,
  "runtime": "uv",
  "auto_retry": {"oom": {"max_attempts": 2}},
  "aggregate_defaults": {
    "require_outputs": "results/{run_id}/metrics.{task_id}.json",
    "expect_output": "results/{run_id}/metrics.json"
  }
}
```

- `cmd_sha` is computed by `hpc_mapreduce.job.runs.compute_cmd_sha`:
  `SHA-256(join("\n", json.dumps(tasks.resolve(i), sort_keys=True) for i in range(tasks.total())))`.
  It is stable across equivalent task lists and changes whenever `tasks.py`
  changes the kwargs returned by `resolve`.
- The user's task definition lives in `.hpc/tasks.py` (a Python module
  exposing `total()` and `resolve(task_id)`); the sidecar references it
  but does not duplicate per-task data.
- The block from `cluster` through `aggregate_defaults` is the **v2
  config snapshot**: every successful `/submit` captures the full config
  it ran under so subsequent commands (`/aggregate`, `/status`,
  `/resubmit`, `/campaign`) read context from the sidecar instead of an
  external config file. All v2 fields are optional at write time;
  `read_run_sidecar` backfills missing keys with `None` so callers see a
  uniform shape regardless of when the sidecar was written.
- `campaign_id` tags the run as part of a closed-loop campaign. The
  `HPC_CAMPAIGN_ID` env var is forwarded to the cluster by every
  scheduler template; the user's `tasks.py` reads it back via
  `os.environ` to call `hpc_mapreduce.reduce.history.prior` on the
  campaign's prior iterations.
- Retention: at most `hpc_mapreduce.job.runs.MAX_RUNS` (default 500;
  override via `HPC_MAX_RUNS` env var) sidecars are kept per experiment
  directory. Oldest by mtime are evicted on every write.

When resuming a prior run, `/submit` matches the recomputed `cmd_sha`
against existing sidecars via `find_run_by_cmd_sha` and delegates to
`hpc_mapreduce.job.resubmit.resubmit_plan(task_count=, failed_task_ids=)`
for the failing task IDs; see `slash_commands/commands/submit.md` for
the interactive resume-vs-fresh prompt.

---

## `python -m hpc_mapreduce.reduce.status`

Emit a full status report for a run.

| Arg | Required | Description |
|---|---|---|
| `--run-id` | yes | Run identifier; locates `.hpc/runs/<run_id>.json` |
| `--job-ids` | no | Comma-separated scheduler job IDs |
| `--job-name` | no | Job name (for error-log lookup) |
| `--scheduler` | no | `sge` or `slurm` (auto-detected if omitted) |
| `--file-glob` | no | Glob for per-task result files (default `*`) |
| `--log-dir` | no | SLURM log directory |
| `--scratch-dir` | no | SGE scratch log directory |
| `--slurm-cluster` | no | `sacct --clusters` value |
| `--sge-user` | no | `qstat -u` value |
| `--min-rows` | no | CSV min-row threshold (default 0) |

The reporter reads `.hpc/runs/<run_id>.json` for the run sidecar and
imports `.hpc/tasks.py` to derive each task's `result_dir`.

**Stdout JSON schema** — four top-level keys always present, plus
`resource_usage` when scheduler accounting data is available:

```json
{
  "summary": {"complete": 0, "running": 0, "pending": 0, "failed": 0, "unknown": 0},
  "tasks":   {"<tid>": {"status": "complete|running|pending|failed|unknown",
                        "cmd_sha": "<16-hex>|null", "...": "..."}},
  "rollup":  {"<grid_point_key>": {"complete": 0, "running": 0, "pending": 0,
                                   "failed": 0, "unknown": 0, "total": 0}},
  "errors":  [{"code": "...", "detail": "..."}],
  "resource_usage": {"cpu_hours": 0.0, "gpu_hours": 0.0, "tasks_counted": 0}
}
```

- `tasks[tid].cmd_sha` is `null` in the new model — `cmd_sha` lives at
  the run level (sidecar), not per task.
- `resource_usage` is additive and backwards-compatible: derived from
  `sacct`/`qstat` accounting fields (`ElapsedRaw`, `ReqCPUS`, `AllocTRES` for
  SLURM; `ru_wallclock`, `slots`, `gpu` for SGE). Sums across completed tasks
  only. Absent or zeroed when the scheduler query returns no accounting data.
- Exit code: `0` on success, `2` if the sidecar is missing or unparseable.

## `python3 .hpc/_hpc_dispatch.py` (on cluster)

Runs one task. Imports the user's `.hpc/tasks.py`, reads `.hpc/runs/<HPC_RUN_ID>.json`
for the executor command and result_dir template, formats result_dir from
kwargs, and execs the resolved command.

| Env | Required | Description |
|---|---|---|
| `HPC_TASK_ID` | yes | 0-based task index (derived from `SGE_TASK_ID`/`SLURM_ARRAY_TASK_ID`) |
| `HPC_RUN_ID` | yes | Run identifier; locates `.hpc/runs/<run_id>.json` |
| `HPC_TASKS_PATH` | no | Override path to tasks.py (default sibling of dispatch.py) |

Exit codes: `0` on task success, non-zero on setup or dispatch failure.

Stderr messages that `/status` greps for:
- `HPC_TASK_ID env var not set`
- `HPC_RUN_ID env var not set`
- `tasks.py not found`
- `run sidecar not found`
- `sidecar schema_version=...`

## `python3 .hpc/_hpc_combiner.py` (on cluster)

Aggregates completed tasks belonging to one wave into `_combiner/wave_N.json`.

| Arg | Required | Description |
|---|---|---|
| `--wave` | yes | Wave index `N` (int) |
| `--run-id` | yes | Run identifier; locates `.hpc/runs/<run_id>.json` |
| `--force` | no | Re-run even if `_combiner/wave_N.json` already exists |

Env fallbacks when args are absent: `HPC_WAVE`, `HPC_RUN_ID`.

**Output file** `_combiner/wave_N.json`:

```json
{
  "wave": 0,
  "run_id": "...",
  "task_ids": [0, 1, ...],
  "grid_points": {"<grid_point_key>": {"...": "..."}},
  "errors": ["task 7: metrics.json not found", "..."]
}
```

Exit `0` on success (wave combined), non-zero on failure (e.g. missing task
results, reducer raised). Stderr carries a short excerpt `/status` can show.

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
    *,
    host: str,
    user: str,
    remote_path: str,
    wave: int,
    run_id: str,
    force: bool = False,
    timeout: float | None = ...,
) -> tuple[bool, str, str]:
    """Run the on-cluster combiner and return (ok, stdout, stderr)."""
```

- `ok` is `True` iff the underlying SSH process exits 0.
- `stdout` is the combiner's JSON (when produced) or empty.
- `stderr` should be captured into the `/status` state blob on failure so the
  user can see why a wave did not combine.

## `hpc_mapreduce.reduce.history`

The campaign read-side accessor. Read-only; pure local filesystem walk.

```python
def find_sidecars_by_campaign(
    experiment_dir: Path, campaign_id: str
) -> list[dict[str, Any]]:
    """Sidecars matching campaign_id, oldest-first by mtime."""

def result_dirs_for_sidecar(
    experiment_dir: Path, sidecar: dict[str, Any]
) -> list[Path]:
    """Per-task result dirs for one sidecar — formats the
    result_dir_template against {run_id, task_id} and globs unknown
    {var} placeholders. Does NOT import .hpc/tasks.py."""

def prior(
    experiment_dir: Path, campaign_id: str
) -> list[dict[str, Any]]:
    """Per-iteration reduced-metric dicts, oldest-first. Each entry is
    reduce_metrics() over the iteration's result dirs. Pending
    iterations contribute {} so callers can filter with `not entry`."""
```

The non-import-tasks.py guarantee matters because closed-loop callers
invoke `prior()` from inside their own `tasks.py` module body; an inner
load would deadlock or recurse.

## `hpc_mapreduce.campaign.run_campaign`

The asyncio in-flight queue. Maintains *concurrency* live submits via
fully-injected callbacks.

```python
async def run_campaign(
    *,
    concurrency: int,
    submit_one: Callable[[], Awaitable[str]],          # returns run_id
    await_completion: Callable[[str], Awaitable[None]],
    should_submit: Callable[[], Awaitable[bool] | bool],
    on_event: Callable[[dict[str, Any]], None] | None = None,
    wall_clock_budget_seconds: float | None = None,
) -> CampaignResult:
    """Drive a closed-loop campaign with at most *concurrency* live submits.

    Stops when should_submit returns False (user's tasks.py signals
    termination via total() == 0) or wall_clock_budget_seconds elapses.
    Iteration failures land as on_event entries with `error`; the loop
    continues so a single bad iteration doesn't bring down the campaign.
    """
```

`CampaignResult` carries `completed: list[str]`, `terminated_reason: str`
(`"tasks_exhausted" | "wall_clock_budget" | "cancelled"`),
`iterations_submitted: int`, `iterations_completed: int`,
`elapsed_seconds: float`. See `slash_commands/commands/campaign.md` and
`docs/campaign.md` for the calling convention and recipe library.
