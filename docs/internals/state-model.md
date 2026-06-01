# State model — what gets persisted where

hpc-agent treats **on-disk state as the single source of truth** for
run state. Conversational memory is not used: a context compaction,
session restart, or fresh chat doesn't lose anything. This doc is the
canonical reference for what files exist, what each contains, and
which primitives read/write them.

## Per-user state (`~/.claude/hpc/<repo_hash>/`)

Everything below lives under
`~/.claude/hpc/<repo_hash>/` where `<repo_hash>` is the SHA of the
experiment repo's absolute path. The hash makes the state
per-checkout: two different clones of the same repo get separate
state.

### `runs/<run_id>.json` — per-run sidecar

The canonical record of a single submission. Written at submit time;
updated as lifecycle transitions happen.

```json
{
  "run_id": "ml_ridge_20260101_120000_abcd1234",
  "cluster": "hoffman2",
  "profile": "ml_ridge",
  "submitted_at_iso": "2026-01-01T12:00:00Z",
  "cmd_sha": "5ac46c384ebb3202",
  "job_ids": ["1234567", "1234568", ...],
  "resources": {"walltime_sec": 7200, "memory_mb": 8000, "gpu_type": "a100"},
  "env": {"modules": [...], "conda_env": "..."},
  "constraints": {...},
  "remote_path": "/scratch/user/ml_ridge_20260101_120000_abcd1234",
  "campaign_id": null,
  "wave_map": {...},
  "lifecycle_state": "running",
  "complete_count": 47,
  "total_tasks": 100,
  "failed_task_ids": [],
  "schema_version": 2
}
```

Read by: `load-context`, `find-prior-run`, `monitor-flow`,
`aggregate-flow`, `resubmit`, every campaign primitive.

Written by: `submit-flow` (initial), `monitor-flow` (lifecycle
transitions), `aggregate-flow` (post-aggregation marker), `resubmit`
(adds resubmit wave records).

### `journal.jsonl` — append-only submission log

Every submission attempt gets a line, regardless of whether it
succeeded or was deduped against a prior run. Used for history,
forensics, dedup.

```jsonl
{"event": "submit_attempt", "cmd_sha": "5ac46c384ebb3202", "run_id": "...", "deduped": false, "at_iso": "..."}
{"event": "submit_attempt", "cmd_sha": "5ac46c384ebb3202", "run_id": "...", "deduped": true, "at_iso": "..."}
{"event": "lifecycle_transition", "run_id": "...", "from": "running", "to": "complete", "at_iso": "..."}
```

Read by: `find-prior-run`, `monitor-summary`, audit tooling.

Written by: every primitive that touches a run's lifecycle.

### `preflight_<cluster>.json` — 24h cache marker

After a green `check-preflight` run, this marker says "the cluster
environment was healthy at `checked_at`; subsequent submits within 24
hours can skip the re-probe." Read by `/submit-hpc`'s Step 6b gate.

```json
{
  "checked_at": "2026-01-01T12:00:00Z",
  "all_ok": true,
  "cluster": "hoffman2"
}
```

Read by: `submit-flow` Step 6b gate.

Written by: `hpc-agent setup --cluster <name>` after a green probe.

### `campaigns/<slug>/` — campaign cursor + manifest

Per-campaign state. The slug matches `^[A-Za-z0-9._\-]+$`.

```
campaigns/<slug>/
├── manifest.json         # campaign metadata (path A/B, slug, created_at, ...)
├── cursor.json           # current iteration index + history
├── iteration_N/          # per-iteration data (one dir per tick)
│   ├── tasks.py.snapshot # exact tasks.py used this iteration
│   ├── interview.json    # any human-resolved decisions
│   └── result.json       # the iteration's aggregated metrics
```

Read by: `campaign-driver`, `load-context`, `campaign-status`.

Written by: `campaign-advance` (on each tick), `campaign-init`.

### `runtime_priors/<profile>__<cluster>__<cmd_sha>.json` — historical walltimes

Quantile rollup of observed task walltimes for a specific
`(profile, cluster, cmd_sha)` combination. Used by an optional plugin's
walltime-right-sizing and by the core's `validate-walltime-against-history`.

```json
{
  "profile": "ml_ridge",
  "cluster": "hoffman2",
  "cmd_sha": "5ac46c384ebb3202",
  "n_samples": 247,
  "quantiles": {"p50": 145, "p75": 220, "p95": 410, "p99": 580},
  "last_updated_iso": "2026-01-01T12:00:00Z"
}
```

Read by: `read-runtime-prior` (optional plugin), `validate-walltime-against-history`, `plan-submit` (optional plugin).

Written by: `aggregate-flow`'s runtime-sample ingestion step.

### `squeue_snapshots/<YYYYMMDDTHHMMSS>.tsv.gz` (optional plugin only)

Cron-collected snapshots of `squeue` output, used to train the
LightGBM-residual queue-wait predictor. Only present when an optional
plugin's `install-cron` has been run.

Read by: `train_wait_predictor` (the nightly trainer), `predict-start-time` (the residual lookup).

Written by: `snapshot_squeue` cron entry.

### `wait_predictor/` (optional plugin only)

The persisted LightGBM model + feature list + training summary.
Written by the nightly trainer; read by `predict-start-time` at
inference time.

```
wait_predictor/
├── model.lgb              # serialized LightGBM regressor
├── feature_names.json     # ordered list of feature names
├── training_summary.json  # last training run's quality metrics
└── drift_history.jsonl    # rolling history of training quality across runs
```

## Per-experiment state (`<experiment>/.hpc/`)

Lives in the user's experiment repo, alongside their source code.
Version-controlled by convention (the user commits these files).

### `tasks.py` — the task generator

User-authored (with framework scaffolding via `build-tasks-py`).
Defines `total()` and `resolve(task_id)`. See
[`parallelization-axes.md`](parallelization-axes.md) Axis 1.

### `axes.yaml` — scheduling + classification

Two unrelated blocks share this file:

```yaml
# Scheduling axes (Axis 2 in parallelization-axes.md)
homogeneous_axes:
  - seed
  - window
axes:
  seed: 100
  model: 4
  window: 2

# DataAxis classification per @register_run function (Axis 5)
executors:
  forecast:
    run_signature_sha: "abc123..."
    data_axis:
      kind: "bounded_halo"
      halo:
        expr: "train_window * 48"
    classified_by: "interview"   # or "recall" or "agent"
    classified_at: "2026-01-01T12:00:00Z"
```

The two blocks are orthogonal; the `axes` block is informational
cardinality data, `homogeneous_axes` is the scheduling pick,
`executors.<run>.data_axis` is the per-run DataAxis. They share the
file by convention.

Read by: `submit-flow` (scheduling); `classify-axis` (for cache check); `axes-init` (for the homogeneous_axes side); `hpc-axes-init` slash + skill.

Written by: `axes-init` primitive (homogeneous side); `classify-axis` primitive (DataAxis side).

### `interview.json` — onboarding outputs + memory

Records the conversational onboarding's outputs: which entry point was
chosen, the task_generator shape, the goal, the operator. Also stores
the elision-equivalent fixture path if one was registered.

```json
{
  "goal": "scale 100-seed forecast across windows",
  "task_count": 800,
  "produced_by": {"kind": "human", "operator": "alice"},
  "task_generator": {"kind": "cartesian_product", "params": {...}},
  "entry_point": {"kind": "register_run", "run_name": "forecast"},
  "_materialized": {
    "tasks_py_path": "...",
    "cmd_sha": "5ac46c384ebb3202",
    "at": "2026-01-01T12:00:00Z"
  },
  "transcript": [...]
}
```

Read by: `submit-flow` (Step 0b — honors materialized entry_point), `recall` (for memory across campaigns), `hpc-wrap-entry-point` skill.

Written by: `interview` primitive.

### `wrappers/<run_name>.py` — wrapper materialization (fallback path only)

For non-Python or decorator-conflicting entry points, the
`hpc-wrap-entry-point` skill materializes a `@register_run` wrapper
that subprocess-invokes the user's real command. Lives here.

Read by: framework's `@register_run` discovery (in `experiment_kit/discover.py`).

Written by: `interview` primitive when `entry_point.kind == "shell_command"`.

### `playbook.yaml` — project-level validation policy

User-authored. Encodes known-bad combinations and walltime rules for
`validate-campaign`.

```yaml
known_bad_combinations:
  - gpu: v100
    workload_tag: attn-fp32
    severity: error
    reason: "V100 fp32 attention is numerically unstable"

walltime_rules:
  - below_quantile: 0.95
    severity: warning
    message: "Requested walltime is below historical p95"
```

Read by: `validate-campaign`.

Written by: humans (committed to the experiment repo).

### `runs/<run_id>/` — local mirror of per-task scratch

When a run completes, its scratch outputs (per-task metrics sidecars,
combiner partials) are rsync'd back from cluster scratch to this
local directory.

```
runs/<run_id>/
├── _combiner/             # per-wave combiner outputs
│   ├── wave_0.json
│   ├── wave_1.json
│   └── ...
├── tasks/                 # per-task metrics sidecars
│   ├── task_0.json
│   └── ...
└── aggregated.json        # final reduced metrics
```

Read by: `aggregate-flow` (combiner + reducer); user analysis code.

Written by: `aggregate-flow`'s rsync pull step.

### `.build-cache.json` — `export-package` cache

Content-hash cache so re-exporting notebooks → `src/` is a no-op when
nothing changed. Internal optimization.

## Where each primitive reads/writes

A reverse index — given a primitive, which state files it touches.

| Primitive | Reads | Writes |
|---|---|---|
| `load-context` | `runs/`, `journal.jsonl`, `campaigns/`, `preflight_*.json`, `axes.yaml`, `interview.json` | — |
| `find-prior-run` | `runs/`, `journal.jsonl` | — |
| `setup` (with `--cluster`) | `clusters.yaml` | `~/.claude/`, `preflight_<cluster>.json` |
| `check-preflight` | `clusters.yaml` | — |
| `interview` | — | `tasks.py`, `interview.json`, optionally `wrappers/<name>.py` |
| `build-tasks-py` | — | `tasks.py` |
| `axes-init` | `tasks.py` | `axes.yaml` (homogeneous side) |
| `classify-axis` | `axes.yaml` | `axes.yaml` (executors side) |
| `classify-axis-easy` | source code | — (pure query) |
| `export-package` | notebooks/, `.build-cache.json` | `src/`, `.build-cache.json` |
| `submit-flow` | `tasks.py`, `axes.yaml`, `runs/`, `interview.json`, `preflight_<cluster>.json` | `runs/<id>.json`, `journal.jsonl`, cluster scratch |
| `verify-canary` | `runs/<id>.json` | (cluster-side only) |
| `monitor-flow` / `status` | `runs/<id>.json`, scheduler state | `runs/<id>.json` (lifecycle updates) |
| `reconcile` | `runs/<id>.json`, scheduler state | `runs/<id>.json` |
| `aggregate-flow` | cluster scratch, `runs/<id>.json` | `runs/<id>/`, `runtime_priors/`, `runs/<id>.json` |
| `combine-wave` | per-task sidecars | `_combiner/wave_N.json` |
| `verify-aggregation-complete` | `runs/<id>/`, scheduler state | — |
| `resubmit` | `runs/<id>.json` | `runs/<id>.json` (resubmit wave) |
| `recall` | `campaigns/`, `interview.json` files in sibling experiments | — |
| `campaign-init` / `campaign-advance` | `campaigns/<slug>/` | `campaigns/<slug>/` |
| `campaign-status` / `campaign-list` | `campaigns/` | — |
| `install-cron` (plugin) | — | user crontab |
| `predict-start-time` (plugin) | `runtime_priors/`, `squeue_snapshots/`, `wait_predictor/` | — |
| `validate-campaign` | `tasks.py`, dataset paths, `runtime_priors/`, `playbook.yaml` | — |

## Invariants

A few cross-cutting properties worth knowing:

- **The sidecar is the canonical run record.** If a sidecar exists, the run existed. If the lifecycle_state in the sidecar is `complete`, aggregation is safe.
- **`cmd_sha` is the idempotency key.** Two submissions with the same `cmd_sha` are duplicates; the second is deduped against the first.
- **The journal is append-only.** Never edited or compacted in normal operation.
- **The preflight marker has a 24h TTL.** After 24h, the `/submit-hpc` Step 6b gate forces a re-probe.
- **Campaign cursor advances by one per tick.** Each `hpc-campaign-driver` invocation either advances the cursor or fails; it never advances by more than one.
- **Stage outputs land in shared cluster scratch.** Inter-stage data flow is purely filesystem; no journal entries between stages.

## Locations summary

| Lives at | Scope | Contents |
|---|---|---|
| `~/.claude/hpc/<repo_hash>/` | Per-user, per-repo | Run sidecars, journal, preflight cache, campaign state, runtime priors, (plugin) snapshots + model |
| `<experiment>/.hpc/` | Per-experiment (version controlled) | `tasks.py`, `axes.yaml`, `interview.json`, wrappers, `playbook.yaml`, run output mirrors |
| `<cluster>:<scratch>/<run_id>/` | Per-run, on cluster | Per-task working dirs, combiner partials, deploy bundle |
| `~/.claude/{commands,skills}/` | Per-user | Installed slash commands + skills (placed by `install-commands`) |

## See also

- [`parallelization-axes.md`](parallelization-axes.md) — how the five axes use this state
- [`submit-sequence.md`](submit-sequence.md) — end-to-end walkthrough showing state transitions
- [`adding-a-primitive.md`](adding-a-primitive.md) — recipe for adding a new primitive that touches state
