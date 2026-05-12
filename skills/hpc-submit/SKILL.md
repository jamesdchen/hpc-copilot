---
name: hpc-submit
description: "Submit a parameter-grid experiment to a SLURM/SGE cluster via SSH and record it in the journal. End-to-end pipeline (rsync + deploy + qsub + record) in one CLI call."
allowed-tools: Bash Read Write
---

Agent-facing composition over the **[submit-flow](../../docs/primitives/submit-flow.md) workflow atom** (full pre-flight + rsync + deploy + qsub + record pipeline in one CLI call). For just the journal-write half (when the agent has already qsubbed), use the [submit-spec](../../docs/primitives/submit-spec.md) primitive directly. Both are idempotent on `run_id`: a replay returns `data.deduped: true` and emits no cluster-side side effects.

Throughout this skill, "invoke <primitive>" means call the primitive's `backed_by.cli` or `backed_by.python` entry point; see `docs/primitives/<name>.md` for the full contract. For envelope/exit-code shapes see `docs/reference/cli-spec.md`.

## Setup

Read cluster definitions:
- `clusters.yaml`: resolve path via `python -c 'from claude_hpc import _PACKAGE_ROOT; print(_PACKAGE_ROOT / "config" / "clusters.yaml")'`

Call [suggest-setup-action](../../docs/primitives/suggest-setup-action.md) to figure out where in the priority ladder the experiment sits — it returns `{priority, action, run_id, candidates, reason}`:

```bash
hpc-agent suggest-setup-action --experiment-dir .
```

Branch on `action`:

| `action` | Priority | Meaning | Skill behavior |
|---|---|---|---|
| `monitor` | 0 | At least one in-flight run on the journal | Hand off to `hpc-status` skill with the candidate list. |
| `reuse` | 1 | Per-experiment sidecars exist | Each sidecar carries the full v2 config snapshot — resources/env/constraints/runtime. Reuse keeps `tasks.py` byte-identical so `cmd_sha` matches. |
| `interview` | 2 | `.hpc/tasks.py` exists, no run history | Skip executor-discovery + axes interview (tasks.py already encodes the axis); jump to Step 4b (planner). |
| `fresh` | 3 | Nothing exists | Full interview from Step 1. |

## Step 1: Discover Executors

Invoke [discover-executors](../../docs/primitives/discover-executors.md). The primitive scans `executors/`, `scripts/`, `src/` (in order, falling back to repo root), filters utilities, and classifies each executor by contract.

Map flag set per contract:
- **New-contract** (`info.has_compute_function == true`): if `.hpc/tasks.py` exists, read `FLAGS[<module>]` for the per-executor flag list. If first submit, capture intended flags during Step 6b interview.
- **Old-contract** (`info.has_main_guard` only): run `python3 <info.path> --help` to map the CLI interface.

If `discover_executors` returns empty, the slash command surfaces a scaffolding sub-interview to the user; that dialog is human UX (it lives in the slash) but the actual work it does — copy `claude_hpc/mapreduce/templates/scaffolds/executor_template.py` to a user-chosen path, then walk through `compute(args)` — is what `hpc-build-executor` skill encodes.

## Step 2: Parse user intent

The slash command parses the user's natural-language request into a list of `(executor_id, axis_shape)` tuples. This skill receives the result; flags `--no-canary` and `campaign_id=<slug>` thread through verbatim.

For multi-executor submissions sharing `(ssh_target, remote_path)`, build a **batch spec**: `{"specs": [<per-spec>...], "rsync_excludes": [...], "skip_preflight": ...}`. `submit-flow` auto-routes to `submit-flow-batch` internally — ONE rsync + ONE deploy + N qsubs over the multiplexed ssh ControlMaster. Heterogeneous batches raise `spec_invalid`. The motivation: N parallel single-spec submits send ~13×N ssh handshakes at the cluster's sshd and trip `MaxStartups`.

## Step 3: Plan the parallelization axis

The task list lives in user-written `.hpc/tasks.py` (`total()` + `resolve(task_id)`). Step 6 walks the user through writing it once per experiment, adapting from `claude_hpc/mapreduce/templates/scaffolds/tasks_example.py`. From then on the file is committed and reused on every submit.

Step 3's job is to gather enough context for Step 6 to write a sensible first draft: axis shape, kwargs `resolve(task_id)` returns, expected task count.

If the projected task count exceeds `constraints.max_tasks` or ~1000, the slash command surfaces a confirm prompt before proceeding.

## Step 4: Auto-Configure Environment

Resolve in order: cluster (interactive or `--cluster`); `SSH_TARGET` + `REMOTE_PATH` from cluster config; environment classification from `info.imports`:

| Imports detected | Classification | Environment |
|---|---|---|
| `torch`/`tensorflow`/`cuda` | GPU/DL | Load CUDA modules + activate conda env |
| `sklearn`/`xgboost`/`lightgbm` | CPU/ML | Load python modules |
| `numpy`/`pandas` only | CPU/lightweight | Load python modules |

For DL executors with `conda_envs` listed in `clusters.yaml` → present the options; without → ask. Resource defaults: CPU/ML 1×16G×4h; GPU/DL 4×16G×6h×2gpu (gpu_type=first in cluster's `gpu_types`).

Build rsync excludes from `.gitignore` patterns + standard set (`__pycache__/`, `*.pyc`, `.git/`, `.claude/`, `.mypy_cache/`) + result directories. **`.hpc/` rides rsync** — the cluster needs `tasks.py` and the in-flight `runs/<run_id>.json`. The framework files inside cluster-side `.hpc/` (`_hpc_dispatch.py`, `_hpc_combiner.py`, `templates/`) are placed by `deploy_runtime` and protected from `--delete` via `DEFAULT_RSYNC_EXCLUDES` in `claude_hpc.infra.remote`.

## Step 4b: Compute Throughput Plan

After grid expansion produces `total_tasks`:

1. Load constraints: `from claude_hpc import ClusterConstraints, parse_constraints`; merge cluster + per-profile.
2. Build workload: `from claude_hpc.planning.throughput import WorkloadSpec, compute_submission_plan`.
3. `compute_submission_plan(constraints, workload)` returns a `SubmissionPlan` with batched waves.
4. Embed `wave_map = build_wave_map(plan)` — passed to `write_run_sidecar(..., wave_map=wave_map)` at Step 6d. The cluster-side combiner reads it from there.

If constraints are not configured for the cluster/profile, skip and submit as a single array.

## Step 4c: Smart constraint planner (resource-quality aware)

For GPU profiles, invoke [score-submit-plan](../../docs/primitives/score-submit-plan.md). For CPU-only, skip.

Optional pre-check: [best-submit-window](../../docs/primitives/best-submit-window.md) (`hpc-agent best-submit-window --profile <p> --cluster <c> --within-hours 24 --top-k 5`) surfaces low-traffic windows. Advisory; the slash command decides whether to surface "submit now vs wait" to the user.

Three branches on `score-submit-plan`'s envelope:

### 4c-A: `needs_canary: true` (cold start)

No runtime priors exist. Don't try to score — submit a 1-task canary first using `data.canary_plan.constraint`. Run through Steps 5–10 with `--no-canary` (we **are** the canary). Wait for terminal; capture `gpu_type`, `node`, `elapsed_sec`, `exit_code` from sacct/qacct. On success, append a sample via `claude_hpc.state.runtime_prior.append_sample`. On SEGV: STOP and surface to user (do NOT auto-retry on a different node — the failure is informative; re-running blindly may mask whether the workload itself is buggy). On timeout: bump walltime 2× and retry the canary ONCE. After two timeouts surface to user.

After a *successful* canary, re-invoke score-submit-plan and proceed to 4c-B.

### 4c-B: `needs_canary: false` (priors exist)

Score per the rubric in [score-submit-plan.md](../../docs/primitives/score-submit-plan.md). **Adversarial backfill mode** is default-on:

1. **Walltime shrink** — recommend p95 × 1.30 from `runtime_prior.elapsed_sec` (≥5 samples per GPU type).
2. **Footprint shrink** — recommend `--mem` from `peak_host_mem_mb` (p95 × 1.50, ≥10 samples) and `--cpus-per-task` from `cpu_seconds_used / elapsed_sec`. Both axes only **shrink below** the user's defaults — never grow — to avoid silent OOM/cliff kills.
3. **Probe lattice** — sweep `(walltime × mem × constraint)` via `sbatch --test-only` and pick the variant SLURM predicts will start earliest.

**Auto-pick rule** (per-candidate): when `recommended_tuple.predicted_eta_sec is not None`, use the tuple's walltime/mem/cpus/constraint automatically. SLURM has confirmed a fitting backfill window; we take it. Surface `rationale` to the audit file so the choice is replayable.

**Auto-apply rule** (cluster-wide): apply `array_reshape.recommended_max_array_size` automatically when present. Do NOT auto-apply `walltime_split` — confirm with user that the executor checkpoints before chaining (split's `requires_checkpointing: true` would otherwise kill work at every segment boundary).

**Closed-loop calibration**: `plan-submit` reads recent samples and tunes the walltime safety multiplier. Top-level `walltime_drift` field reports `{base_safety_mult, adjusted_safety_mult, rationale}`. After submission, write a prediction sidecar via `claude_hpc.forecast.calibration.record_prediction_sidecar` so post-completion ingestion can validate calibration.

For each chosen candidate's `stressed_nodes`, the SLASH command (not the skill) decides per-node whether to soft-exclude using `co_tenants` context — that's the human-judgment moment that no static threshold captures cleanly. The skill receives the resulting `--exclude=<node1>,...` flag and adds it to the sbatch invocation.

### 4c-C: planner errors

If `plan-submit` envelope is `ok: false`, fall back to static-constraint flow: take `gpu_constraint` and `constraints.max_walltime` from `clusters.yaml`, proceed without exclude list. Surface the planner error verbatim — the user knows quality awareness is degraded.

### Audit file

After Step 8 returns job IDs, write the decision to `.hpc/runs/<run_id>.decision.json`:

```python
import json
from pathlib import Path
from datetime import datetime, timezone
decision = {
    "schema_version": 1,
    "run_id": run_id,
    "profile": profile,
    "cluster": cluster,
    "submitted_at": datetime.now(timezone.utc).isoformat(),
    "candidates_considered": [...],
    "chosen": {"constraint": ..., "walltime_sec": ..., "exclude_nodes": [...], "rationale": ...},
    "job_ids": job_ids,
}
Path(f".hpc/runs/{run_id}.decision.json").write_text(json.dumps(decision, indent=2, sort_keys=True))
```

## Step 5: Confirm Run Plan (via summarize-submit-plan)

Don't hand-author the summary. Once Step 6c emits the resolved spec via [build-submit-spec](../../docs/primitives/build-submit-spec.md), render the canonical confirmation via [summarize-submit-plan](../../docs/primitives/summarize-submit-plan.md):

```bash
hpc-agent summarize-submit-plan --spec /tmp/submit_spec.json
```

The envelope's `data` carries `{headline, body, confirm_prompt}`. Print `headline` and `body` verbatim, then ask `confirm_prompt`. For multi-job submissions, call once per spec and concatenate bodies under one combined header. The primitive flips to a magnitude-warning prompt automatically when `total_tasks > 1000`.

## Step 6: Scaffold (or reuse) `.hpc/tasks.py` and write the per-run sidecar

### 6a: Reuse if `.hpc/tasks.py` exists

```python
from pathlib import Path
from claude_hpc import (
    framework_subdir, tasks_path, load_tasks_module, compute_cmd_sha,
)
experiment_dir = Path.cwd()
framework_subdir(experiment_dir)
tp = tasks_path(experiment_dir)
```

If `tp.exists()`, read it as-is — never regenerate. To change the axis, the user edits `.hpc/tasks.py` directly and re-runs. Skip to 6c.

### 6b: Scaffold from canonical example (first submit only)

If `tp.exists()` is False, walk through `claude_hpc/mapreduce/templates/scaffolds/tasks_example.py` (top-level `FLAGS: dict[str, list[Flag]]`, eager-materialized `_TASKS = [...]`, three commented-out usage patterns inline). Generate via [build-tasks-py](../../docs/primitives/build-tasks-py.md) — don't hand-author it. Refuses to overwrite without `--force`.

**Axis naming (fidelity vs. serial)**: when the user proposes axis names, prefer experiment-prefixed forms (`exp_horizon`, `ridge_alpha`) over bare names (`horizon`, `alpha`). The dispatcher exports each kwarg as both `HPC_KW_<KEY>` and bare `<KEY>` (uppercased), and the bare form silently shadows real env vars when names collide — an axis named `home` becomes `$HOME` for the executor, breaking everything that uses the home directory. `build-tasks-py` rejects names that match a reserved set (`HOME`, `PATH`, `USER`, `LD_LIBRARY_PATH`, `OMP_NUM_THREADS`, the framework's own `HPC_*`, scheduler-injected `SLURM_*`/`SGE_*`/`PBS_*`, etc.) so the failure surfaces at scaffold time, but the safest pattern is to prefix all experiment kwargs and avoid the question. Setting `HPC_KW_NAMESPACE_ONLY=1` in the spec's `job_env` disables the bare-uppercase export entirely (executor reads `HPC_KW_<KEY>` only) and is the recommended default for new campaigns.

Copy the dispatcher:
```python
import shutil
from claude_hpc import _PACKAGE_ROOT
shutil.copy(_PACKAGE_ROOT / "mapreduce" / "templates" / "scaffolds" / "cli_dispatcher.py", experiment_dir / ".hpc" / "cli.py")
```

Commit `.hpc/tasks.py` + `.hpc/cli.py`. No push — user controls upstream.

### 6c: Compute `cmd_sha`, check for resume

```python
from claude_hpc import compute_cmd_sha, compute_tasks_py_sha
tasks = load_tasks_module(tp)
cmd_sha = compute_cmd_sha(tasks)
tasks_py_sha = compute_tasks_py_sha(tp)
```

```bash
hpc-agent find-prior-run --experiment-dir . --cmd-sha "$CMD_SHA"
```

Branch on envelope's `{found, is_orphan}`:
- `found=False` → fresh; continue to 6d.
- `found=True, is_orphan=False` → real prior. Slash command asks user "Resume or fresh?"
- `found=True, is_orphan=True` → half-baked sidecar. Suggest `prune-orphan-sidecars` or proceed and let `submit_flow_batch`'s auto-prune handle it.

### 6d: Write sidecar + build submit-flow spec

Use [build-submit-spec](../../docs/primitives/build-submit-spec.md) to assemble the spec — synthesizes `EXECUTOR`/`HPC_RUN_ID`/`HPC_CMD_SHA`/`HPC_TASK_COUNT`/`REPO_DIR`/`MODULES`/`CONDA_SOURCE`/`CONDA_ENV`/`HPC_RUNTIME`/`HPC_CAMPAIGN_ID`, picks the canonical script path from `(backend, is_gpu)`, validates against `schemas/submit_flow.input.json`.

Write the per-run sidecar via `write_run_sidecar(..., wave_map=wave_map)`. Pass `None` for any v2 field that doesn't apply. **Don't pass `job_ids` here** — the sidecar is *pending* until `submit-flow` runs `update_run_sidecar_job_ids` after qsub returns.

## Step 6b: Pre-flight Gate (cached per cluster)

Cache marker: `~/.claude/hpc/<repo_hash>/preflight-<cluster>.json` (TTL 24h). If marker exists, `all_ok=true`, < 24h old → log `preflight: cached <N>m ago — OK` and skip to Step 7.

Otherwise invoke [check-preflight](../../docs/primitives/check-preflight.md) with `--cluster <name>`. On `data.all_ok == true`: write/update marker, continue. On any check failure: do NOT write marker, surface failing checks verbatim, stop.

## Step 6c: Pre-submit campaign validation

Invoke `validate-campaign`:

```bash
hpc-agent validate-campaign --spec validate_campaign.input.json --experiment-dir .
```

Branch on `data.overall`:
- `pass` → proceed.
- `warn` → surface warnings; proceed unless user explicitly fixes first.
- `fail` → do NOT proceed. List `error`-severity findings with `code`/`message`/`suggested_fix`, apply fixes, re-run. **No `--force` flag by design** — edit `.hpc/playbook.yaml` if a rule is wrong.

## Step 6d: Predict start time

Invoke [predict-start-time](../../docs/primitives/predict-start-time.md). Inputs: squeue + sshare snapshots (gather via SSH first), partition info, your priority/walltime/constraint, candidate offsets `[0,1,3,6,12,24]`. Surface result:
- `best_submit_offset_hours == 0` → submit now is optimal.
- `> 0` → suggest "wait N hours, predicted total time M minutes vs submit-now's M' minutes" — slash command renders the user prompt.

Advisory, NOT a gate. The skill always proceeds; the predictor is decision support.

## Step 7-8: Invoke `submit-flow`

Steps 7 (rsync), 7b (canary), 8 (qsub), 10 (record) are ONE CLI call. Spec shape (matches `schemas/submit_flow.input.json`):

```json
{
  "profile": "<job_name>", "cluster": "<cluster>", "ssh_target": "user@host",
  "remote_path": "<remote_path>", "job_name": "<job_name>",
  "run_id": "<run_id from 6d>", "total_tasks": <tasks.total()>,
  "backend": "sge_remote", "script": ".hpc/templates/cpu_array.sh",
  "job_env": {"EXECUTOR": "python3 .hpc/_hpc_dispatch.py", "HPC_RUN_ID": "...", ...},
  "pass_env_keys": [...],
  "canary": true, "campaign_id": "<slug>", "runtime": "uv",
  "skip_preflight": true
}
```

`skip_preflight: true` is correct — Step 6b just ran. For GPU jobs: `script: ".hpc/templates/gpu_array.sh"` (SGE) or `gpu_array.slurm` (SLURM).

```bash
hpc-agent submit-flow --spec spec.json --experiment-dir .
```

- `data.deduped: true` → original cluster jobs running. Switch to `hpc-status` skill.
- `data.deduped: false` → fresh. Capture `data.run_id`/`job_ids`/`canary_job_ids`.
- Error envelopes: branch by `error_code` per submit-flow's contract.

### Canary verification (route through `verify-canary`)

When `data.canary_done: true`:

```bash
hpc-agent verify-canary --experiment-dir . --canary-run-id "$CANARY_RUN_ID" --expect-output "results/seed_42/metrics.json"
```

Branch:
- `ok=True` → continue to main array submit.
- `ok=False` → surface `stderr_tail` verbatim. `failure_kind` tags the category (`dispatcher_failed`/`import_error`/`oom_killed`/`missing_output`/`timeout`).

## Step 8b: Verify the array is queued/running

`qsub`/`sbatch` returning a job ID is necessary but not sufficient. Confirm each returned job ID is alive on the cluster BEFORE reporting success:

```bash
# SLURM
ssh $SSH_TARGET 'squeue -j '"$JOB_IDS"' -h -o "%i %T %r"; sacct -j '"$JOB_IDS"' -n -P -o JobID,State,Reason 2>&1 | head'
# SGE
ssh $SSH_TARGET 'qstat -j '"$JOB_IDS"' 2>&1 | head -40; qstat -u '"$USER"' | awk "NR>2"'
```

**Healthy** (proceed): SLURM `PENDING`/`RUNNING`/`CONFIGURING`/`COMPLETING`; SGE `qw`/`hqw`/`r`/`t`/`Rq`/`Rr`. Wave-2+ jobs `PENDING Reason=Dependency` (SLURM) / `hqw` (SGE) are healthy.

**Failed** (abort): SLURM `BOOT_FAIL`/`FAILED`/`NODE_FAIL`/`OUT_OF_MEMORY`/`TIMEOUT`/`DEADLINE`/`REVOKED`/`SPECIAL_EXIT`, or `CANCELLED` within seconds of submit. SGE state starting with `E` or `d`. Job ID absent from both `squeue`/`qstat` and `sacct`/`qacct` after one retry: scheduler never registered it.

On failure: surface scheduler reason verbatim, tell user which job ID is bad, stop. Do not run Step 9 or Step 10.

## Step 9-10: Cache + report

Cache to Claude Code memory: executor directory, cluster, remote_path, env config per executor type, default resources.

Report after submission and Step 8b verification: job ID, executor(s), grid dimensions, total tasks, cluster, verified scheduler state. Suggest `/monitor-hpc` to track progress.

The journal write happens inside `submit-flow` via `runner.submit_and_record`. For multi-executor submissions (one sidecar per executor), invoke `submit-flow` once per submitted job — each call writes its own sidecar.

## Notes

- **SSH env passthrough**: caller must forward `SSH_AUTH_SOCK` and `SSH_AGENT_PID` or every cluster call hangs on auth. Run `hpc-preflight` first.
- **Scheduler rate limits**: serialize submits to a single cluster; most schedulers cap at ~1/sec. Sleep 1s between back-to-back calls or expect `scheduler_throttled`.
- **Idempotency**: `submit-flow` is replay-safe on `run_id`. If `data.deduped: true`, original cluster jobs are running — do NOT re-invoke.
- **No cancel/abort**: claude-hpc has no kill primitive. If you decide an experiment is bad, stop monitoring; cluster jobs run to walltime.
- `--dry-run` never touches the cluster and never writes to the journal — safe to run repeatedly.
- The cluster-side template translates the scheduler's per-task index (`SGE_TASK_ID` / `SLURM_ARRAY_TASK_ID`) into `HPC_TASK_ID` (0-based) before exec'ing `$EXECUTOR`, which then imports `.hpc/tasks.py`, calls `tasks.resolve(HPC_TASK_ID)`, and runs the executor command from the sidecar with kwargs merged into the env.
