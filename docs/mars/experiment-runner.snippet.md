# Cluster Execution (claude-hpc) — paste into `agents/experiment-runner.md`

This file is a paste-ready section for the MARs `experiment-runner`
agent. It assumes claude-hpc has been added to MARs's `pyproject.toml`
(`uv add claude-hpc`) and that the spawn env forwards `SSH_AUTH_SOCK`,
`SSH_AGENT_PID`, `HPC_JOURNAL_DIR`, `HPC_CLUSTERS_CONFIG`, and `PATH`.
See [claude-hpc's mars-integration.md](https://github.com/jamesdchen/claude-hpc/blob/main/docs/mars-integration.md)
for the `Bun.spawn` block.

The snippet preserves MARs's existing rules: `uv run` for all Python,
seed=42 from `meta.json`, output to `results/metrics.json`. claude-hpc
is **opt-in per run** and never used for Tier-1 probes.

---

```markdown
## Cluster Execution (Optional)

When a Tier-2 run requires HPC scale (grid > 32 tasks, per-task
walltime > 30 minutes, GPU contention on local hardware), delegate
cluster submission to `hpc-mapreduce`. **Tier-1 probes always run
locally** with `uv run python probe.py`; never invoke `hpc-mapreduce`
for a probe.

Decision rule: estimate the grid before submitting. If
`executors × params ≤ 8` AND total walltime fits a single local
GPU/CPU, run locally. Else delegate.

### Pre-flight (run once per session)

```bash
uv run hpc-mapreduce preflight --cluster <name>
```

Parse the JSON envelope. If `data.all_ok` is false, surface
`data.checks[]` to the user and stop. Common failure: `ssh_auth_sock`
is false → the spawn env is missing `SSH_AUTH_SOCK`. This is the
operator's problem, not a code bug.

### Scaffold `.hpc/tasks.py` (you write this; claude-hpc imports it)

This is the central agent-driven moment. The framework's task fan-out
is defined by **`<experiment-dir>/.hpc/tasks.py`** — a small Python
module with two callables:

```python
def total() -> int: ...               # number of tasks
def resolve(i: int) -> dict: ...      # kwargs for task #i
```

Claude (you) writes this file once per experiment proposal,
translating `meta.json`'s parameter axes into a materialized `_TASKS`
list. The framework never auto-generates it — keeping the experiment
definition in user code (committed to git) is what makes claude-hpc
reusable across experiments.

1. If `.hpc/tasks.py` already exists, **do not regenerate**. Verify it
   imports cleanly and `total()` returns the cardinality you expect:
   ```bash
   uv run python -c 'from claude_hpc import load_tasks_module, tasks_path; m = load_tasks_module(tasks_path(".")); print("total=", m.total(), "sample=", m.resolve(0))'
   ```
   Skip to "Build the run spec" below.

2. Otherwise, read the canonical reference (the only `tasks.py` example
   the framework ships):
   ```bash
   uv run python -c 'from claude_hpc import _PACKAGE_ROOT; print(_PACKAGE_ROOT / "mapreduce" / "templates" / "tasks_example.py")'
   ```
   It demonstrates three patterns inline (Cartesian product, chunking,
   date-window backtests). Pick the one that matches what `meta.json`
   describes; delete the rest.

3. Translate `meta.json`'s axes into `_TASKS`. Eager-materialized — the
   list is built at module load, not on each `resolve()` call. Example
   for a `{lr: [0.01, 0.001], seed: [42, 1337]}` sweep:
   ```python
   # .hpc/tasks.py
   import itertools
   _TASKS = [
       {"lr": lr, "seed": seed}
       for lr, seed in itertools.product([0.01, 0.001], [42, 1337])
   ]
   def total() -> int:    return len(_TASKS)
   def resolve(i: int) -> dict: return _TASKS[i]
   ```

4. Verify locally before submitting:
   ```bash
   uv run python -c 'from claude_hpc import load_tasks_module, tasks_path, compute_cmd_sha; m = load_tasks_module(tasks_path(".")); print("total=", m.total(), "cmd_sha=", compute_cmd_sha(m)[:8])'
   ```

5. Commit `.hpc/tasks.py` alongside `meta.json` and your executor:
   ```bash
   git add .hpc/tasks.py meta.json scripts/<executor>.py
   git commit -m "scaffold experiment <experiment_id>"
   ```

### (Optional) Probe queue wait before submitting

If you want to choose a low-latency window for a long-running submit,
ask the predictor:

```bash
uv run hpc-mapreduce best-submit-window --profile <experiment_id> --cluster <name> --within-hours 6
```

Returns the top-K windows by predicted wait time. With cold-start
data (`confidence: "cold"`), submit immediately; otherwise pick a
window and wait. This is opt-in — never required.

### Build the run spec

The submit-spec is the JSON envelope passed to `hpc-mapreduce submit`.
It carries the run's identity (`run_id`), cluster routing, and task
count derived from `tasks.total()`:

```json
{
  "profile": "<experiment_id>",
  "cluster": "hoffman2",
  "ssh_target": "user@hoffman2.idre.ucla.edu",
  "remote_path": "/u/scratch/<user>/<experiment_id>",
  "job_name": "<experiment_id>",
  "run_id": "<experiment_id>-<utc_ts>-<cmd_sha8>",
  "job_ids": [],
  "total_tasks": <tasks.total()>
}
```

Construct `run_id` as `f"{experiment_id}-{utc_ts}-{cmd_sha[:8]}"`
where `cmd_sha` is from `compute_cmd_sha(tasks_module)`. This format
sorts chronologically and ties identity to the materialized task list
— a re-run of the same experiment with unchanged `tasks.py` produces
the same `cmd_sha` (and `submit` will dedup on it).

Validate before submitting:

```bash
uv run hpc-mapreduce submit --spec spec.json --dry-run
```

### Submit

```bash
uv run hpc-mapreduce submit --spec spec.json
```

Parse the envelope:

- `data.deduped: true` — a journal record for this `run_id` exists;
  the cluster jobs are already running. Do NOT re-issue `qsub`. Switch
  to `status` polling.
- `data.deduped: false` — fresh submission. Record `data.run_id` and
  `data.job_ids` for downstream calls.

### Status polling

```bash
uv run hpc-mapreduce status --run-id <run_id>
```

Read `data.lifecycle_state`:

- `in_flight`: keep polling, backoff 30s → 60s → 120s.
- `complete`: proceed to `aggregate`.
- `failed`: inspect `data.last_status` for failed task counts; decide
  whether to `resubmit` or surface to the user.
- `timeout`: a poll-deadline elapsed without a terminal state. Re-poll
  with a longer deadline, OR call `reconcile` to reconcile the journal
  against scheduler reality.
- `abandoned`: scheduler shows no live jobs but the run was not marked
  complete. Run `reconcile` and inspect.

Also surface from `data` (top-level): `preempted_count` and
`preempted_task_ids` — tasks that exited with the canonical
preemption signal (exit 130). These are NOT failures; the cluster
bumped them. Selectively resubmit just those task_ids via:

```bash
uv run hpc-mapreduce resubmit --run-id <run_id> --task-ids <comma-list> --category preempted
```

### Aggregate per wave

```bash
uv run hpc-mapreduce aggregate --run-id <run_id> --wave <int>
```

After all waves are combined, read the per-task outputs from
`<experiment-dir>/_aggregated/<run_id>/` and assemble
`results/metrics.json` in MARs's canonical schema (`experiment_id`,
`timestamp`, `seed`, `models`, `rankings`, `statistical_tests`).

### Error handling

| `error_code`            | Action                                                       |
|-------------------------|--------------------------------------------------------------|
| `ssh_unreachable`       | Halt-and-prompt; do not loop. Re-run preflight after fix.    |
| `scheduler_throttled`   | Backoff 1s → 2s → 4s, max 4 retries. Schedulers cap at 1/s.  |
| `cluster_timeout`       | Backoff 4s → 8s → 16s, max 3 retries.                        |
| `combiner_failed`       | Single retry after inspecting `stderr_tail`; else surface.   |
| `preempted`             | Resubmit with `--category preempted` immediately. The job was bumped, not failed. |
| `cluster_partially_degraded` | Inspect top-level `partial_errors`; continue polling. The cluster is responding but a sub-system is timing out. |
| `remote_command_failed` | Surface with `stderr_tail`; do not auto-retry.               |
| `spec_invalid`          | Surface; the spec is wrong. Regenerate it.                   |
| `executor_not_found`    | Surface; check executor path under `scripts/`.               |
| `cluster_unknown`       | Surface; run `clusters list` to recover.                     |
| `config_invalid`        | Surface; clusters.yaml is malformed.                         |
| `outputs_missing`       | Surface; the executor produced no per-task outputs.          |
| `journal_corrupt`       | Surface; investigate `$HPC_JOURNAL_DIR`.                     |
| `schema_incompat`       | Surface; pin claude-hpc and the cluster runtime to compatible versions. |

Exit codes: 0 ok, 1 user error (fix and retry), 2 cluster/network (per
`retry_safe`), 3 internal (bug report).

### Constraints (from claude-hpc)

- **No cancel/abort.** Once submitted, jobs run to walltime;
  claude-hpc cannot kill them. If you decide a run is bad, stop polling
  and let it expire.
- **Submit is idempotent on `run_id`.** A retried submit with the same
  `run_id` returns `deduped: true`.
- **Resubmit is idempotent on `request_id`.** A second call with the
  same spec returns `deduped: true` without incrementing per-task
  retry counters. When the caller does not supply a `request_id`, one
  is derived from `(failed_task_ids, category, overrides)`. Use
  `list-in-flight` to inspect retry counters.
- **Idempotency-skip on resubmit.** If a task's `result_dir/metrics.json`
  exists with non-zero size, the cluster-side dispatcher exits 0
  without re-running the executor. Convention: executors that don't
  call `claude_hpc.mapreduce.metrics_io.write_metrics` won't get free
  skip-on-resubmit.
- **Scheduler rate limits.** Serialize submissions to a single
  cluster.
- **`HPC_JOURNAL_DIR` is per-MARs-run.** Set it to
  `~/.mars/hpc/<experiment_id>/` so concurrent runs don't share state.
```

---

That's the entire paste. The block is roughly 110 lines of markdown.
