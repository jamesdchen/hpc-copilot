# Cluster Execution (claude-hpc) — paste into `agents/experiment-runner.md`

This file is a paste-ready section for the MARs `experiment-runner` agent.
It assumes claude-hpc has been added to MARs's `pyproject.toml` (`uv add
claude-hpc`) and that the spawn env forwards `SSH_AUTH_SOCK`,
`SSH_AGENT_PID`, `HPC_JOURNAL_DIR`, `HPC_CLUSTERS_CONFIG`, and `PATH`. See
[claude-hpc's mars-integration.md](https://github.com/jamesdchen/claude-hpc/blob/main/docs/mars-integration.md)
for the Bun.spawn block.

The snippet preserves MARs's existing rules: `uv run` for all Python,
seed=42 from `meta.json`, output to `results/metrics.json`. claude-hpc is
**opt-in per run** and never used for Tier-1 probes.

---

```markdown
## Cluster Execution (Optional)

When a Tier-2 run requires HPC scale (grid > 32 tasks, per-task wall > 30
minutes, GPU contention on local hardware), delegate cluster submission
to `hpc-mapreduce`. **Tier-1 probes always run locally** with `uv run
python probe.py`; never invoke `hpc-mapreduce` for a probe.

Decision rule: estimate the grid before submitting. If `executors × params
≤ 8` AND total walltime fits a single local GPU/CPU, run locally. Else
delegate.

### Pre-flight (run once per session)

```bash
uv run hpc-mapreduce preflight --cluster <name>
```

Parse the JSON envelope. If `data.all_ok` is false, surface
`data.checks[]` to the user and stop. Common failure: `ssh_auth_sock` is
false → the spawn env is missing `SSH_AUTH_SOCK`. This is the operator's
problem, not a code bug.

### Build the grid spec

Read `meta.json` first. The spec MUST include `seed: 42` and
`experiment_id: <from meta.json>`. Example:

```json
{
  "profile": "<experiment_id>",
  "cluster": "hoffman2",
  "ssh_target": "user@hoffman2.idre.ucla.edu",
  "remote_path": "/u/scratch/<user>/<experiment_id>",
  "job_name": "<experiment_id>",
  "run_id": "<experiment_id>-<utc_ts>-<cmd_sha8>",
  "job_ids": [],
  "total_tasks": 0
}
```

`run_id` is the primary identity field. It locates the per-run sidecar
at `.hpc/runs/<run_id>.json` once the agent has scaffolded
`.hpc/tasks.py` and submitted (see `/submit` Step 6). The legacy
`manifest_filename` field is still accepted by the schema for
back-compat, but `run_id` should be preferred for new callers.

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
- `abandoned`: scheduler shows no live jobs but the run was not marked
  complete. Run `reconcile` and inspect.

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
| `remote_command_failed` | Surface with `stderr_tail`; do not auto-retry.               |
| `manifest_invalid`      | Surface; the spec is wrong. Regenerate.                      |
| `executor_not_found`    | Surface; check executor path under `scripts/`.               |
| `cluster_unknown`       | Surface; run `clusters list` to recover.                     |
| `config_invalid`        | Surface; clusters.yaml or hpc.yaml is malformed.             |
| `journal_corrupt`       | Surface; investigate `$HPC_JOURNAL_DIR`.                     |

Exit codes: 0 ok, 1 user error (fix and retry), 2 cluster/network (per
`retry_safe`), 3 internal (bug report).

### Constraints (from claude-hpc)

- **No cancel/abort.** Once submitted, jobs run to walltime; claude-hpc
  cannot kill them. If you decide a run is bad, stop polling and let it
  expire.
- **Submit is idempotent on `run_id`.** A retried submit with the same
  `run_id` returns `deduped: true`.
- **Resubmit is idempotent on `request_id`.** A second call with the same
  spec returns `deduped: true` without incrementing per-task retry
  counters. When the caller does not supply a `request_id`, one is
  derived from `(failed_task_ids, category, overrides)`. Use
  `list-in-flight` to inspect retry counters.
- **Scheduler rate limits.** Serialize submissions to a single cluster.
- **`HPC_JOURNAL_DIR` is per-MARs-run.** Set it to
  `~/.mars/hpc/<experiment_id>/` so concurrent runs don't share state.
```

---

That's the entire paste. The block is roughly 90 lines of markdown.
