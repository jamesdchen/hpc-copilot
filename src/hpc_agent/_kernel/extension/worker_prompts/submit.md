Agent-facing composition over the **[submit-flow](../../docs/primitives/submit-flow.md) workflow atom** (full pre-flight + rsync + deploy + qsub + record pipeline in one CLI call). For just the journal-write half (when the agent has already qsubbed), use the [submit-spec](../../docs/primitives/submit-spec.md) primitive directly. Both are idempotent on `run_id`: a replay returns `data.deduped: true` and emits no cluster-side side effects.

Throughout this procedure, "invoke <primitive>" means call the primitive's `backed_by.cli` or `backed_by.python` entry point; see `docs/primitives/<name>.md` for the full contract. For envelope/exit-code shapes see `docs/reference/cli-spec.md`.

## Setup

**Load context first.** Run `hpc-agent load-context --experiment-dir .` and treat its `data` as the ONLY source of truth for run / campaign / cluster state. Never rely on conversational memory or shell variables — a context compaction or a session restart erases them; the on-disk state does not.

- `data.latest_run` — cluster, profile, resources, env, remote_path, campaign_id, run_id, cmd_sha, job_ids. On a `reuse`/`interview` action, read these instead of re-interviewing the user.
- `data.in_flight` — active runs (run_id, stage, ssh_target, job_ids).
- `data.campaigns` — campaign ids + cursor iteration.
- `data.next_step_hint` — `submit` / `monitor` / `aggregate`.

If a value you need later is absent here, derive it from the run sidecar on disk — never from memory.

Read cluster definitions:
- `clusters.yaml`: resolve path via `python -c 'from hpc_agent import _PACKAGE_ROOT; print(_PACKAGE_ROOT / "config" / "clusters.yaml")'`

Call [suggest-setup-action](../../docs/primitives/suggest-setup-action.md) to figure out where in the priority ladder the experiment sits — it returns `{priority, action, run_id, candidates, reason}`:

```bash
hpc-agent suggest-setup-action --experiment-dir .
```

Branch on `action`:

| `action` | Priority | Meaning | Procedure behavior |
|---|---|---|---|
| `monitor` | 0 | At least one in-flight run on the journal | Stop and report; the caller switches to the status workflow. |
| `reuse` | 1 | Per-experiment sidecars exist | Each sidecar carries the full v2 config snapshot — resources/env/constraints/runtime. Reuse keeps `tasks.py` byte-identical so `cmd_sha` matches. |
| `interview` | 2 | `.hpc/tasks.py` exists, no run history | Skip executor-discovery + axes interview (tasks.py already encodes the axis); jump to Step 4b. |
| `fresh` | 3 | Nothing exists | Full interview from Step 1. |

## Step 0: Build the `src/` package

The experiment repo commits **nothing generated** — `src/` is `.gitignore`d. Build it from the notebooks before anything else, so discovery, the elision gate, and the deploy bundle all see a current package:

```bash
hpc-agent export-package --experiment-dir .
```

`export-package` globs `notebooks/{pipeline,executors,scripts}/*.ipynb`, exports each to `src/<module>.py` (strict-AST for `@register_run` executors, `# export`-marker for pipeline libraries), and content-hash-caches against `.hpc/.build-cache.json` — a no-op when nothing changed. The built `src/` rides the `submit-flow` rsync into the deploy bundle; **the cluster node never builds** (it stays stdlib-only). On a `spec_invalid` envelope (an output-path collision, a bad module name), surface it and stop — the notebooks need a rename.

## Step 0b: Honor `interview.json` entry_point (if present)

Before Step 1's `@register_run` discovery, check whether the campaign was set up via the `interview` primitive. If `<experiment_dir>/interview.json` exists, read its `_materialized.entry_point` block — it tells the worker the entry point was *declared* rather than *discovered*, which short-circuits several later steps:

```bash
test -f interview.json && python -c "
import json, sys
doc = json.load(open('interview.json'))
ep = doc.get('_materialized', {}).get('entry_point')
if ep: print(json.dumps(ep))
" | tee /tmp/_entry_point.json
```

If the block is non-empty, branch on `kind`:

| `kind` | Procedure behavior |
|---|---|
| `shell_command` | A wrapper has been materialized at `<wrapper_path>` (`.hpc/wrappers/<run_name>.py`) — it satisfies the `@register_run` contract. **Skip Step 1's discover scan**; treat `<run_name>` as the picked run. **Use `<executor_cmd>` as the `EXECUTOR` in Step 6d's `job_env`** instead of synthesizing one from `discover_executors`. If `data_axis` is on the block, **skip the Step 3b classification interview** — the user pre-declared the axis; just feed `data_axis` into the `axes.yaml` write that classify-axis would have done. `tasks.py` is already on disk (the interview materialized it from `task_generator`) — Step 6a's reuse branch picks it up. |
| `python_module` | The entry point is an importable Python module. Skip Step 1's discovery scan; treat `<module>:<function>` as the picked run. The `EXECUTOR` at Step 6d is `python3 -m <module>` (or a one-liner that imports `<function>`). |
| `register_run` | A `@register_run` function name is declared, but no wrapper was materialized — fall through to Step 1's discovery, scoped to `<run_name>` rather than enumerating. |

If `interview.json` doesn't exist, or `_materialized.entry_point` is absent, the rest of the procedure runs unchanged (the notebook-discovery default).

This step is what makes the mature-repo path end-to-end usable: the `interview` primitive set up the wrapper and persisted the executor command; this step reads them and threads them into the rest of the submit pipeline so nothing is orphaned.

## Step 1: Discover runs

**Notebook-first.** The researcher authors only a notebook carrying a `@register_run def run(...)` — no axis declaration, no `tasks.py`, no CLI glue. Discovery is `discover_runs` over `notebooks/`, which AST-walks `.py` and `.ipynb` files (skipping `.hpc/`):

```bash
python .hpc/scaffold.py discover
```

Each line is `<path>::<name>  gpu=<bool>  sha=<run_signature_sha>  flags=[...]`.

- **Bare `/submit-hpc`** (the default) — list every `@register_run` and let the user pick one.
- **`/submit-hpc <notebook>`** — scope discovery to that one file.

Record the picked run's `name`, `gpu`, `flags`, and `run_signature_sha` — the signature hash is the cache key for Step 3's classification lookup.

For environment classification (Step 4) you still need the run's imports; invoke [discover-executors](../../docs/primitives/discover-executors.md) for the matching module's `info.imports` / `info.has_compute_function`, or read the notebook's import cells directly.

### Step 1b: Discover Executors (legacy / env detail)

Invoke [discover-executors](../../docs/primitives/discover-executors.md). The primitive scans `executors/`, `scripts/`, `src/` (in order, falling back to repo root), filters utilities, and classifies each executor by contract.

Map flag set per contract:
- **New-contract** (`info.has_compute_function == true`): if `.hpc/tasks.py` exists, read `FLAGS[<module>]` for the per-executor flag list. If first submit, capture intended flags during Step 6b interview.
- **Old-contract** (`info.has_main_guard` only): run `python3 <info.path> --help` to map the CLI interface.

If `discover_executors` returns empty, scaffolding requires an interactive sub-interview which a headless worker cannot run — record the boundary in `decisions` and stop for the caller to handle.

## Step 2: Parse user intent

The caller has already parsed the user's natural-language request into a list of `(executor_id, axis_shape)` tuples; the result arrives via the invocation `fields`. Flags `--no-canary` and `campaign_id=<slug>` thread through verbatim.

For multi-executor submissions sharing `(ssh_target, remote_path)`, build a **batch spec** — `{"specs": [<per-spec>...], "rsync_excludes": [...], "skip_preflight": ...}`; `submit-flow` auto-routes it to the batched path (one rsync + one deploy + N qsubs). Heterogeneous batches raise `spec_invalid`. Why batch rather than N parallel submits: see [submit-flow.md](../../docs/primitives/submit-flow.md).

## Step 3: Plan the parallelization axis

The task list lives in user-written `.hpc/tasks.py` (`total()` + `resolve(task_id)`). Step 6 scaffolds it once per experiment; from then on it is committed and reused on every submit. There are two shapes, and Step 3 decides which:

- **Cartesian grid** — each task is one independent cell of a parameter grid. `tasks_example.py` Pattern 1; scaffolded deterministically by [build-tasks-py](../../docs/primitives/build-tasks-py.md) at Step 6b. The 80% case.
- **Planner-driven** — the executor iterates a *totally-ordered series* (a walk-forward backtest, an online-learning scan) and you want to fan that series out. Splitting a *stateful* series computation is only correct if each chunk replays the right warm-up; hpc-agent owns that via `hpc_agent.experiment_kit.plan_tasks`. Emitted by [build-tasks-py](../../docs/primitives/build-tasks-py.md) when the spec carries a `data_axis` (Step 3b's classification).

### 3a: Detect a series axis

Read `compute()` / the `@register_run` function and its call graph — the same code-analysis pass that classifies hardware from `info.imports` at Step 4. A series axis is present when the executor loops over an ordered series (a time index, a date range, rows of a sorted frame) and you intend to parallelize *that loop*. If there is no series loop, it is a cartesian grid — skip to Step 4.

### 3b: Classify the `DataAxis` — cache check, then interview

The experiment declares nothing about parallelism — the classification is stored in `<experiment>/.hpc/axes.yaml`'s `executors.<run_name>` block, keyed by run name and stamped with the `run_signature_sha` it was classified against.

**Cache lookup.** Read `axes.yaml`. If `executors.<run_name>` exists **and** its `run_signature_sha` equals the picked run's current `run_signature_sha` (from Step 1) → the stored `DataAxis` is still valid: **reuse it, skip the interview.** A mismatch (signature drifted) or no entry → conduct the classification interview.

**Interview.** The `hpc-classify-axis` skill walks the proposes-then-confirms decision tree with the user — that interaction needs a human, which a headless worker cannot give. Record the boundary in `decisions` / `anomalies` and stop; the caller resolves the classification, writes it to `axes.yaml`, and re-invokes this workflow.

> **`DataAxis` ≠ scheduling axes.** `axes.yaml` holds two unrelated things: the `executors.<run>.data_axis` block (this step — *how to split the series correctly*) and `homogeneous_axes` / `axes` (Step 4b / `hpc-axes-init` — *which sweep dimension goes on the task array*). They are orthogonal; classifying the `DataAxis` never touches the scheduling axes.

### 3c: Serial-elision gate (mandatory for a non-`Sequential` axis)

Before scaffolding a planner-driven `tasks.py`, prove the classification on a fixture: `hpc_agent.experiment_kit.check_elision` (or `assert_elision_equivalent`) runs the experiment once whole and once split N ways and asserts the results agree. If it fails, the axis is misclassified — widen the halo or fall back to `Sequential()`. This gate is what makes the inference safe: a misclassified axis produces a job that runs fine and returns plausible-but-wrong numbers, and nothing else catches it. Do not skip it, and recommend the experiment repo wire `assert_elision_equivalent` into its CI as a required check.

If the projected task count exceeds `constraints.max_tasks` or ~1000, record a `magnitude_warning` in `decisions` / `anomalies` so the caller can confirm with the user before proceeding.

## Step 4: Auto-Configure Environment

Resolve in order: cluster (from `fields` or `data.latest_run`); `SSH_TARGET` + `REMOTE_PATH` from cluster config; environment classification from `info.imports`:

| Imports detected | Classification | Environment |
|---|---|---|
| `torch`/`tensorflow`/`cuda` | GPU/DL | Load CUDA modules + activate conda env |
| `sklearn`/`xgboost`/`lightgbm` | CPU/ML | Load python modules |
| `numpy`/`pandas` only | CPU/lightweight | Load python modules |

For DL executors with `conda_envs` listed in `clusters.yaml` → record the candidates as a `decisions` entry for the caller to confirm with the user; the caller re-invokes with the picked env in `fields`. Resource defaults: CPU/ML 1×16G×4h; GPU/DL 4×16G×6h×2gpu (gpu_type=first in cluster's `gpu_types`).

Build rsync excludes from `.gitignore` patterns + the standard set (`__pycache__/`, `*.pyc`, `.git/`, `.claude/`, `.mypy_cache/`) + result directories.

**Do not exclude the generated package.** The scaffolded `.gitignore` lists `src/`, `.hpc/tasks.py`, and `.hpc/cli.py` — they are generated, not committed — but the cluster node *needs* them: `src/` is the executor package built at Step 0, and `tasks.py`/`cli.py` are the dispatch contract. When deriving excludes from `.gitignore`, **drop `src/`, `.hpc/tasks.py`, and `.hpc/cli.py` from the exclude list** so the built bundle ships them. `.hpc/` rides rsync generally — the cluster also needs the in-flight `runs/<run_id>.json`; `submit-flow` protects the framework-deployed `.hpc/` files from `--delete` itself (see [submit-flow.md](../../docs/primitives/submit-flow.md)). Do keep excluding `.hpc/.build-cache.json` — it is a local-build artifact the node never reads.

## Step 4b: Compute Throughput Plan

After grid expansion produces `total_tasks`, invoke [plan-throughput](../../docs/primitives/plan-throughput.md):

```bash
hpc-agent plan-throughput --cluster <name> --total-tasks <n> [--est-task-duration-s <s>]
```

It reads the cluster's scheduler constraints from `clusters.yaml`, packs the grid into concurrency-bounded waves, and returns `{strategy, total_batches, n_waves, est_total_wall_s, wave_map, ...}`. Thread the returned `wave_map` into `write_run_sidecar(..., wave_map=wave_map)` at Step 6d — the cluster-side combiner reads it from the sidecar. A cluster with no `constraints:` block falls back to scheduler defaults (a single array for a grid under the default `max_array_size`).

## Step 5: Confirm Run Plan (via summarize-submit-plan)

Don't hand-author the summary. Once Step 6c emits the resolved spec via [build-submit-spec](../../docs/primitives/build-submit-spec.md), render the canonical confirmation via [summarize-submit-plan](../../docs/primitives/summarize-submit-plan.md):

```bash
hpc-agent summarize-submit-plan --spec /tmp/submit_spec.json
```

The envelope's `data` carries `{headline, body, confirm_prompt}`. Surface `headline`, `body`, and `confirm_prompt` in the worker `result` so the caller can show them to the user. For multi-job submissions, call once per spec and concatenate bodies under one combined header. The primitive flips to a magnitude-warning prompt automatically when `total_tasks > 1000`.

## Step 6: Scaffold (or reuse) `.hpc/tasks.py` and write the per-run sidecar

### 6a: Reuse if `.hpc/tasks.py` exists

```python
from pathlib import Path
from hpc_agent import RepoLayout, load_tasks_module
from hpc_agent.state.runs import compute_cmd_sha

experiment_dir = Path.cwd()
layout = RepoLayout(experiment_dir)
_ = layout.hpc  # mkdir's .hpc/ + writes .gitignore on first read
tp = layout.tasks
```

If `tp.exists()`, read it as-is — never regenerate. To change the axis, the user edits `.hpc/tasks.py` directly and re-runs. Skip to 6c.

### 6b: Scaffold from canonical example (first submit only)

If `tp.exists()` is False, walk through `hpc_agent/models/mapreduce/templates/scaffolds/tasks_example.py` (top-level `FLAGS: dict[str, list[Flag]]`, eager-materialized `_TASKS = [...]`, three commented-out usage patterns inline). Generate via [build-tasks-py](../../docs/primitives/build-tasks-py.md) — don't hand-author it. Refuses to overwrite without `--force`.

**Planner-driven axis (Step 3b).** When Step 3 classified a non-trivial `DataAxis`, source it from `axes.yaml`'s `executors.<run_name>.data_axis` block (written by `classify-axis`) and pass it to [build-tasks-py](../../docs/primitives/build-tasks-py.md) in the spec's `data_axis` field: `{kind, chunks, series_length, halo_expr?, monoid?}`. The primitive then emits a `plan_tasks`-driven `tasks.py` deterministically — the `axes` become the sweep, the series axis is partitioned per the classification. The agent classifies; it never hand-writes `tasks.py`. `series_length` is the integer you probed at Step 3a; `chunks` is the desired per-sweep-point split count.

> **Halo-expression form differs between the two surfaces.** The `executors` block stores the halo as `halo.expr` over **bare** parameter names (`train_window * 48`). `build-tasks-py`'s `data_axis.halo_expr` expects the **`params[...]`** form (`params['train_window'] * 48`). When threading a stored `bounded_halo` classification into `build-tasks-py`, rewrite each bare name `X` → `params['X']`. Both forms are validated to safe arithmetic before use.

The serial-elision gate (Step 3c) must have passed before the file is committed.

**Axis naming**: prefer experiment-prefixed axis names (`exp_horizon`, `ridge_alpha`) over bare ones (`horizon`, `alpha`) — a bare name whose uppercase form is a real env var (an axis `home` → `$HOME`) corrupts the executor's environment. `build-tasks-py` rejects names that collide with a reserved set at scaffold time; the mechanism and the recommended `HPC_KW_NAMESPACE_ONLY=1` default are in [build-tasks-py.md](../../docs/primitives/build-tasks-py.md).

Copy the dispatcher:
```python
import shutil
from hpc_agent import _PACKAGE_ROOT
shutil.copy(_PACKAGE_ROOT / "models" / "mapreduce" / "templates" / "scaffolds" / "cli_dispatcher.py", experiment_dir / ".hpc" / "cli.py")
```

Commit `.hpc/tasks.py` + `.hpc/cli.py`. No push — user controls upstream.

### 6c: Compute `cmd_sha`, check for resume

```python
from hpc_agent.state.runs import compute_cmd_sha, compute_tasks_py_sha
tasks = load_tasks_module(tp)
cmd_sha = compute_cmd_sha(tasks)
tasks_py_sha = compute_tasks_py_sha(tp)
```

```bash
hpc-agent find-prior-run --experiment-dir . --cmd-sha "$CMD_SHA"
```

Branch on envelope's `{found, is_orphan}`:
- `found=False` → fresh; continue to 6d.
- `found=True, is_orphan=False` → real prior. Record in `decisions` and surface to the caller — only the user can choose resume-vs-fresh.
- `found=True, is_orphan=True` → half-baked sidecar. Suggest `prune-orphan-sidecars` or proceed and let `submit_flow_batch`'s auto-prune handle it.

### 6d: Write sidecar + build submit-flow spec

Use [build-submit-spec](../../docs/primitives/build-submit-spec.md) to assemble the spec — synthesizes `EXECUTOR`/`HPC_RUN_ID`/`HPC_CMD_SHA`/`HPC_TASK_COUNT`/`REPO_DIR`/`MODULES`/`CONDA_SOURCE`/`CONDA_ENV`/`HPC_RUNTIME`/`HPC_CAMPAIGN_ID`, picks the canonical script path from `(backend, is_gpu)`, validates against `schemas/submit_flow.input.json`.

Write the per-run sidecar via `write_run_sidecar(..., wave_map=wave_map)`. Pass `None` for any v2 field that doesn't apply. **Don't pass `job_ids` here** — the sidecar is *pending* until `submit-flow` runs `update_run_sidecar_job_ids` after qsub returns.

## Step 6b: Pre-flight Gate (cached per cluster)

Cache marker: `~/.claude/hpc/<repo_hash>/preflight-<cluster>.json` (TTL 24h). If marker exists, `all_ok=true`, < 24h old → log `preflight: cached <N>m ago — OK` and skip to Step 7.

Otherwise invoke [check-preflight](../../docs/primitives/check-preflight.md) with `--cluster <name>`. On `data.all_ok == true`: write/update marker, continue. On any check failure: do NOT write marker, record `setup_required` in `decisions` with the failing checks verbatim and stop — the user fixes their environment with `hpc-agent setup --cluster <name>` and the caller re-invokes.

## Step 6c: Pre-submit campaign validation

Invoke `validate-campaign`:

```bash
hpc-agent validate-campaign --spec validate_campaign.input.json --experiment-dir .
```

Branch on `data.overall`:
- `pass` → proceed.
- `warn` → record warnings in `anomalies`; proceed.
- `fail` → do NOT proceed. Record the `error`-severity findings with `code`/`message`/`suggested_fix` in `decisions` and stop. **No `--force` flag by design** — the caller edits `.hpc/playbook.yaml` if a rule is wrong, then re-invokes.

## Step 7-8: Invoke `submit-flow`

Steps 7 (rsync), 7b (canary), 8 (qsub), 10 (record) are ONE CLI call. Spec shape (matches `schemas/submit_flow.input.json`):

```json
{
  "profile": "<job_name>", "cluster": "<cluster>", "ssh_target": "user@host",
  "remote_path": "<remote_path>", "job_name": "<job_name>",
  "run_id": "<run_id from 6d>", "total_tasks": <tasks.total()>,
  "backend": "sge", "script": ".hpc/templates/cpu_array.sh",
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

- `data.deduped: true` → original cluster jobs running. Record `deduped` in `decisions`; the caller switches to the status workflow.
- `data.deduped: false` → fresh. Capture `data.run_id`/`job_ids`/`canary_job_ids`.
- Error envelopes: branch by `error_code` per submit-flow's contract.

### Canary verification (route through `verify-canary`)

When `data.canary_done: true`:

```bash
hpc-agent verify-canary --experiment-dir . --canary-run-id "$CANARY_RUN_ID" --expect-output "results/seed_42/metrics.json"
```

Branch:
- `ok=True` → continue to main array submit.
- `ok=False` → record `stderr_tail` verbatim and the `failure_kind` (`dispatcher_failed`/`import_error`/`oom_killed`/`missing_output`/`timeout`) in `decisions`, stop.

## Step 8b: Verify the array is queued/running

`qsub`/`sbatch` returning a job ID is necessary but not sufficient. Confirm each returned job ID is alive on the cluster BEFORE reporting success:

```bash
# SLURM
ssh $SSH_TARGET 'squeue -j '"$JOB_IDS"' -h -o "%i %T %r"; sacct -j '"$JOB_IDS"' -n -P -o JobID,State,Reason 2>&1 | head'
# SGE
ssh $SSH_TARGET 'qstat -j '"$JOB_IDS"' 2>&1 | head -40; qstat -u '"$USER"' | awk "NR>2"'
```

Classify each job ID as **healthy** (proceed) or **failed** (abort) per the state taxonomy in [scheduler-states.md](../../docs/reference/scheduler-states.md). A wave-2+ job pending on a dependency is healthy.

On a failed state: record the scheduler reason verbatim and the bad job ID in `decisions`, stop. Do not run Step 9 or Step 10.

## Step 9-10: Cache + report

Do not cache run config in conversational memory. `submit-flow` persists the full v2 config snapshot (executor, cluster, remote_path, env, resources) to the run sidecar; any later step recovers it with `hpc-agent load-context`. Conversational memory is lost on context compaction or a session restart — the sidecar is not.

Report after submission and Step 8b verification: job ID, executor(s), grid dimensions, total tasks, cluster, verified scheduler state. The caller suggests `/monitor-hpc` to track progress.

The journal write happens inside `submit-flow` via `runner.submit_and_record`. For multi-executor submissions (one sidecar per executor), invoke `submit-flow` once per submitted job — each call writes its own sidecar.

## Common failure modes

When Step 8b finds a job in a failed state, or a later check surfaces task failures, map the symptom:

| Symptom | Cause | Fix |
|---|---|---|
| `Eqw` state (SGE) | Job error | `qmod -cj <JOBID>` or resubmit |
| `PENDING` (SLURM) for >30 min | Resource unavailable | Check `sinfo`; try a different partition |
| Memory exceeded | Exceeded the memory limit | Resubmit with higher memory |
| Walltime exceeded | Exceeded the time limit | Resubmit with longer walltime |
| `ModuleNotFoundError` | Environment not set up | Check the modules / conda_env |
| rsync / scp transfer failure | SSH key issue | Verify `ssh $SSH_TARGET hostname` first |
| `--<flag>` not recognized | The executor does not accept that argument | Check `--help`; the flag must be in the executor's `FLAGS` / CLI |

If the requested run names a CLI flag the executor does not accept, record it in `decisions` and stop before submitting — a missing flag fails every task in the array.

## Notes

- **SSH env passthrough**: caller must forward `SSH_AUTH_SOCK` and `SSH_AGENT_PID` or every cluster call hangs on auth. The user runs `hpc-agent setup --cluster <name>` once per machine to probe the environment and populate the 24h cache marker Step 6b reads.
- **Scheduler rate limits**: serialize submits to a single cluster; most schedulers cap at ~1/sec. Sleep 1s between back-to-back calls or expect `scheduler_throttled`.
- **Idempotency**: `submit-flow` is replay-safe on `run_id`. If `data.deduped: true`, original cluster jobs are running — do NOT re-invoke.
- **No cancel/abort**: hpc-agent has no kill primitive. If the user decides an experiment is bad, the caller stops monitoring; cluster jobs run to walltime.
- `--dry-run` never touches the cluster and never writes to the journal — safe to run repeatedly.
- The cluster-side template translates the scheduler's per-task index (`SGE_TASK_ID` / `SLURM_ARRAY_TASK_ID`) into `HPC_TASK_ID` (0-based) before exec'ing `$EXECUTOR`, which then imports `.hpc/tasks.py`, calls `tasks.resolve(HPC_TASK_ID)`, and runs the executor command from the sidecar with kwargs merged into the env.
