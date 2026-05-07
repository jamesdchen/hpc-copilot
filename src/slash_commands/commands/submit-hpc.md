Help me submit HPC jobs via SSH. Discovers experiment executors, builds submission plans conversationally, and handles all deployment.

Per-operation contracts (inputs, outputs, error codes, idempotency) live in `docs/primitives/` — this skill composes from those primitives and adds the human-facing interview, confirmation prompts, and decision tables on top. Throughout this file, "invoke <primitive>" means "call the primitive's `backed_by.cli` or `backed_by.python` entry point; see `docs/primitives/<name>.md` for the full contract." For cross-cutting envelope/exit-code shapes see `docs/reference/cli-spec.md`.

All cluster commands run remotely via SSH. Code is synced from the local machine before submission.

## Setup

Read cluster definitions:
- `clusters.yaml`: resolve path via `python -c 'from claude_hpc import _PACKAGE_ROOT; print(_PACKAGE_ROOT / "config" / "clusters.yaml")'`

Check for existing context (in priority order):

**Migration check (legacy `_hpc_dispatch.json`):** Before any of the
priority checks below, look for a top-level `_hpc_dispatch.json` (or
`manifest.<sha8>.json`, or `manifest.json`) in the experiment dir. These
are artifacts of the pre-`.hpc/tasks.py` model that no longer drive the
framework. If any are present, surface a one-time migration message:

> "I found a legacy dispatch manifest at `_hpc_dispatch.json`. The
> framework no longer reads manifests — task definitions live in
> `.hpc/tasks.py` and per-run state in `.hpc/runs/<run_id>.json`. I'll
> walk you through writing `.hpc/tasks.py` once at Step 6 (using your
> existing manifest as a translation hint if helpful), then we can move
> the old manifest aside. OK to proceed?"

If the user agrees, continue to priority 0 below; the manifest's
existing `tasks[*].cmd` and `tasks[*].params` are useful context for
Step 6's scaffolding conversation but are not consumed by the framework.
Once the new `.hpc/tasks.py` is committed, suggest the user `git mv
_hpc_dispatch.json .hpc/legacy/` (or simply delete it). Don't proceed
silently — a stale `_hpc_dispatch.json` next to a fresh `.hpc/tasks.py`
is confusing on inspection.

**Don't walk the priority list by hand. Call `suggest-setup-action`** — it runs all four checks and returns the recommended action + candidates verbatim:

```bash
hpc-agent suggest-setup-action --experiment-dir .
```

The envelope's `data` carries `{priority, action, run_id, candidates, reason}`. Branch on `action`:

| `action` | Priority | Meaning | What to do |
|---|---|---|---|
| `monitor` | 0 | At least one in-flight run on the journal | Surface `candidates` to the user. Group by `campaign_id` if >3 runs. Offer "Resume monitoring with /monitor-hpc, or start a new submission?" |
| `reuse` | 1 | Per-experiment sidecars exist | Surface the recent (profile, cluster) pairs (in `candidates`). Offer "Resubmit same, modify (edit `.hpc/tasks.py`), or start fresh?" Reuse keeps `tasks.py` byte-identical so `cmd_sha` matches; the new sidecar's `run_id` differs but the lineage is preserved. |
| `interview` | 2 | `.hpc/tasks.py` exists, no run history | Skip the executor-discovery + axes interview (tasks.py already encodes the axis); jump to Step 4b (planner). |
| `fresh` | 3 | Nothing exists | Full interview from Step 1. |

   When prompting the user about reuse vs. fresh, list the distinct `(profile, cluster)` pairs from recent run sidecars (via `find_existing_runs(experiment_dir)`) so they can pick "same as last `ml_ridge` submission" without re-answering interview questions. Each sidecar carries the full v2 config snapshot — resources, env, constraints, runtime — so reuse is a one-line copy from the matching sidecar.

## SSH Quoting

Single-quote the remote command so variables expand on the cluster, not locally:

```bash
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && echo $SGE_TASK_ID'
```

## Step 1: Discover Executors

Invoke the [discover-executors](../../docs/primitives/discover-executors.md) primitive (`claude_hpc.discover_executors(".")` returns `list[ExecutorInfo]`). The primitive scans `executors/`, `scripts/`, and `src/` (in that order, falling back to the repo root if none exist), filters out utilities and `__init__.py`, and classifies each executor by contract — see the primitive's Notes for the new-vs-old-contract rules.

Cache the resolved directory in Claude Code memory for this project. If the cached directory differs from the defaults, pass it through `search_dirs=(...)`. If the user explicitly names a different directory, honor it the same way.

How to map each executor's flag set depends on which contract the primitive reported:

- **New-contract** (`info.has_compute_function == true`): if `.hpc/tasks.py` exists, read `FLAGS[<module>]` for the per-executor flag list. If it doesn't exist yet (first submit), capture the intended flags during the Step 6b interview and write them into the FLAGS dict you generate.
- **Old-contract** (`info.has_main_guard` only): run `python3 <info.path> --help` to map the CLI interface — argparse-style scripts respond to `--help` even before the framework is installed.

Parse the typical axes either way:
- Grid-able parameters (model hyperparams, feature types, etc.)
- Data arguments (`--data-path`, `--horizon`, `--start`, `--end`)
- Output arguments (`--output-file` is contractual)

Present the inventory (use `info.name` and `info.path` as identifiers; `info.docstring` is handy for the one-line summary). Examples are illustrative — `/submit-hpc` works with any executor that satisfies either contract.

```
Executors found in src/ (illustrative — names, flags, and domain are per-experiment):
  ml_ridge.py     — flags: --horizon, --data-path, --train-window, --start, --end, --output-file
  ml_xgboost.py   — flags: --horizon, --data-path, --train-window, --start, --end, --output-file
  dl_patchts.py   — flags: --horizon, --data-path, --gpu-count, --start, --end, --output-file

Which do you want to run?
```

If `discover_executors` returns an empty list, pivot to a scaffolding sub-interview right here (this absorbs what `/build-executor-hpc` used to be):

1. Ask: "No executors found in `executors/` / `scripts/` / `src/`. Want me to scaffold one — what should it do?"
2. Copy `claude_hpc/templates/scaffolds/executor_template.py` to a user-chosen path (default: `src/<name>.py`).
3. Walk the user through filling in `compute(args)` based on what they described — model fit/predict, simulation step, data transform, etc.
4. Capture the flag set the user wants (this becomes that executor's entry in the FLAGS dict during Step 6b).
5. Re-run `discover_executors` to confirm the new file is recognized, then continue to Step 2.

## Step 2: Understand User Intent

Parse `$ARGUMENTS` or the user's natural language request:

| User says | Interpretation |
|-----------|---------------|
| "run ridge" | Select `ml_ridge.py` |
| "all ML models" | Select all `ml_*.py` executors |
| "subgroup analysis with ridge and xgboost" | Select `ml_ridge.py` + `ml_xgboost.py`, grid over subgroups |
| "sweep horizons 1, 5, 25 on lightgbm" | Select `ml_lightgbm.py`, fan out over `horizon ∈ [1, 5, 25]` (3 tasks) |

**Flags:**
- `--no-canary` — skip the Step 7b 1-task canary submission. Default behavior is canary-on; only skip when the user has already smoke-tested the pipeline within the last session or is deliberately re-submitting a known-good pipeline.
- `campaign_id=<slug>` (or `--campaign-id <slug>`) — tag this submission as one iteration of a closed-loop campaign. Capture the slug verbatim and pass it to `submit_and_record` in Step 10 so the per-run sidecar carries `campaign_id=<slug>`. Required when invoked as part of `/campaign-hpc`; otherwise omitted (open-loop submissions have empty `campaign_id`). The slug also gets exported to the cluster as `HPC_CAMPAIGN_ID` so the executor and any cluster-side tooling see it.

For multi-executor submissions: submit as **separate array jobs** (independent monitoring and failure handling). Each gets its own `run_id` and per-run sidecar at `.hpc/runs/<run_id>.json`; the same `.hpc/tasks.py` is reused if the parallelization axis matches, otherwise the agent writes a new one (the file is the single seam between executors and the framework).

**For N>1 executors sharing `(ssh_target, remote_path)`, write one batch spec file** instead of N per-executor specs. `submit-flow` auto-dispatches: pass it `{"specs": [...], "rsync_excludes": [...], "skip_preflight": ...}` and it routes to `submit-flow-batch` internally, doing ONE rsync_push + ONE deploy_runtime + N qsubs over the multiplexed ssh ControlMaster. Pass it a single dict and it runs the per-spec pipeline as before. Same `hpc-agent submit-flow --spec X` call either way. All entries under `specs` MUST share `ssh_target` + `remote_path`; heterogeneous batches raise `spec_invalid`. The motivation: N parallel single-spec submits send ~13×N ssh handshakes at the cluster's sshd and trip `MaxStartups` — we've seen 11 parallel campaign submits land 2 successes + 9 SSH timeouts.

## Step 3: Plan the parallelization axis

In the new model, the **task list lives in user-written `.hpc/tasks.py`**: a small Python module exposing `total()` and `resolve(task_id)`. Step 6 walks the user through writing it once per experiment, adapting from the canonical example at `claude_hpc/templates/scaffolds/tasks_example.py`. From then on, the file is committed to git and reused on every submit.

Step 3's job is to gather enough context that Step 6 can write a sensible first draft. From executor CLI args and the user's intent, propose:

- **The shape of the axis**: Cartesian product over named hyperparameters? Chunking by row count? Date-window backtest? Something else?
- **The kwargs `resolve(task_id)` should return**: e.g. `{"seed": ..., "model": ...}` for a grid; `{"chunk_id": ..., "total_chunks": ...}` for chunking; `{"window_start": ..., "window_end": ...}` for backtests.
- **The expected task count** so we can sanity-check before writing.

Present a draft outline:

```
Running ml_ridge.py and ml_xgboost.py.

Proposed parallelization (one tasks.py per executor):
  Axis:        Cartesian product over horizon + chunk-id
  Kwargs:      {"horizon": int, "chunk_id": int, "total_chunks": int}
  Cardinality: 3 horizons × 10 chunks = 30 tasks per executor
  Total:       60 tasks

Adjust the axis, or confirm?
```

If the projected task count (per executor or overall) exceeds the cluster's `constraints.max_tasks` advisory (when set) or a common-sense threshold of ~1000, surface it explicitly: `"This will produce N tasks. Confirm? [y/N]"`. The actual cardinality is whatever `tasks.total()` returns once `.hpc/tasks.py` is written — Step 6 verifies it matches the user's intent before submission.

When the user mentions CLI arguments that the executor doesn't support (e.g., "sweep features=[har, pca]" but `--features` isn't in `--help`), flag it: `"ml_ridge.py doesn't accept --features. Should I add it, or did you mean a different executor?"`.

## Step 4: Auto-Configure Environment

### Cluster Selection
Ask which cluster to use (present options from `clusters.yaml`). Cache in Claude Code memory.

If `$ARGUMENTS` contains `--cluster <name>`, use that cluster.

Construct `SSH_TARGET` (`user@host`) and `REMOTE_PATH` from cluster config.

### Remote Path
Default: `{cluster.scratch}/{project_dir_name}`
Or use cached value from Claude Code memory.
Confirm with user on first submission.

### Environment Detection
Use `info.imports` from the `ExecutorInfo` captured in Step 1 (fall back to reading the source only if that tuple is empty):

| Imports detected | Classification | Environment |
|-----------------|----------------|-------------|
| `torch`, `tensorflow`, `cuda` | GPU / DL | Load CUDA modules, activate conda env |
| `sklearn`, `xgboost`, `lightgbm` | CPU / ML | Load python modules |
| `numpy`, `pandas` only | CPU / lightweight | Load python modules |

Look up the cluster's available modules from `clusters.yaml`.

For DL executors:
- If cluster has `conda_envs` listed → present options: "Available conda envs on hoffman2: [<your_env>, base]. Which one?"
- If no `conda_envs` in config → ask user: "This executor needs a conda environment with PyTorch. What's the env name on {cluster}?"

Cache environment config in Claude Code memory.

### Resource Estimation

| Executor type | Default resources |
|---------------|-------------------|
| CPU / ML | `cpus: 1, mem: "16G", walltime: "4:00:00"` |
| GPU / DL | `cpus: 4, mem: "16G", walltime: "6:00:00", gpus: 2, gpu_type: <first in cluster gpu_types>` |

Present defaults and let user override: "Resources per task: 1 CPU, 16G, 4h. Adjust?"

### Rsync Excludes
Build exclude list from:
1. `.gitignore` patterns (if file exists)
2. Standard patterns: `__pycache__/`, `*.pyc`, `.git/`, `.claude/`, `.mypy_cache/`
3. Result directories (e.g., `results/`)

The local `.hpc/` directory **does** ride rsync (so the cluster receives `tasks.py` and the in-flight `runs/<run_id>.json` sidecar). Don't add `.hpc/` to the exclude list. The framework files inside the cluster-side `.hpc/` (`_hpc_dispatch.py`, `_hpc_combiner.py`, `templates/`) are placed there separately by `deploy_runtime` and are protected from rsync `--delete` via `DEFAULT_RSYNC_EXCLUDES` in `claude_hpc.infra.remote`.

## Step 4b: Compute Throughput Plan

After grid expansion produces total_tasks, compute an optimized submission plan:

1. **Load constraints**: `from claude_hpc import ClusterConstraints, parse_constraints` — read constraints from `clusters.yaml` for the selected cluster, then overlay any per-profile constraints the user supplied in this submit interview (the resolved overrides will be persisted to the run sidecar's `constraints` field).

2. **Build workload**: `from claude_hpc.planning.throughput import WorkloadSpec, compute_submission_plan` — construct a `WorkloadSpec` using `total_tasks` from grid expansion, plus `est_task_duration` if configured in the profile.

3. **Compute plan**: Call `compute_submission_plan(constraints, workload)` to get a `SubmissionPlan` with batched waves.

4. **Display the plan** in the confirmation prompt (Step 5), e.g.:

```
Throughput Plan:
  Strategy:   4 batches (88 tasks each), 2 concurrent, 2 waves, ~30m est.
  Wave 1:     tasks 1-88, 89-176  (submit immediately)
  Wave 2:     tasks 177-264, 265-350  (after wave 1)
```

5. **Embed wave map**: Call `build_wave_map(plan)` to generate a wave-to-task mapping. The map is then passed into `write_run_sidecar(..., wave_map=wave_map)` at Step 6d so it lives in `.hpc/runs/<run_id>.json`. The cluster-side combiner reads it from there to know which tasks belong to each wave.

If constraints are not configured for the cluster or profile, skip this step and submit as a single array (existing behavior).

## Step 4c: Smart constraint planner (resource-quality aware)

The throughput plan from Step 4b decides *batching*; this step decides *which nodes to land on*. Skip for CPU-only profiles (no GPU constraint to choose). For GPU profiles, invoke the [score-submit-plan](../../docs/primitives/score-submit-plan.md) primitive (`hpc-agent plan-submit --profile <profile> --cluster <cluster>`); it combines a live snapshot of the cluster and runtime priors from past runs to score every candidate constraint. Claude then applies the cost rubric below and picks one.

### Optional pre-check: best submit window

Before scoring constraints you can consult [best-submit-window](../../docs/primitives/best-submit-window.md) (`hpc-agent best-submit-window --profile <p> --cluster <c> --within-hours 24 --top-k 5`) to surface low-traffic submit windows in the next 24 hours. This is purely advisory — the primitive sweeps the diurnal queue-wait predictor at hourly offsets and returns the `top_k` lowest-wait candidates. Useful when the user explicitly asks "is now a good time?" or when the current `score-submit-plan` envelope's candidates all carry long predicted waits. The slash command can offer "I see your predicted wait now is 4h; the queue is significantly emptier in 6h. Wait, or submit now?" — but the actual UX is up to the slash command; the primitive just exposes the data. Cold-start clusters return an empty `candidates` array; in that case fall through to the normal submit-now path.

The envelope's `data` carries the candidate scorecards. Three branches:

### 4c-A: `needs_canary: true` (cold start)

No runtime priors exist for this `(profile, cluster)`. Don't try to score — submit a 1-task canary first:

1. Read `data.canary_plan.constraint` (lowest-ETA candidate).
2. Build a normal submission spec with `total_tasks=1` and the canary constraint, run through Steps 5–10 with `--no-canary` (we **are** the canary; nesting a canary inside a canary is double work).
3. Wait for terminal state. Capture `gpu_type`, `node`, `elapsed_sec`, `exit_code` from sacct/qacct.
4. On success, append a runtime sample:

   ```python
   from claude_hpc.state.runtime_prior import append_sample
   append_sample(
       experiment_dir=Path("."),
       profile=profile, cluster=cluster,
       run_id=canary_run_id, task_id=0,
       gpu_type=gpu_type, node=node,
       elapsed_sec=elapsed_sec, exit_code=0,
       cmd_sha=cmd_sha, started_at=..., ended_at=...,
   )
   ```

5. On SEGV: **stop the smart-planning flow** and surface the failure to the user with the canary's stderr tail and the SEGV node. Do NOT auto-retry on a different node (the failure is informative; re-running blindly may mask whether the workload itself is buggy) and do NOT keep looping into a fresh canary (without a successful canary the priors stay empty, so a re-entry would just request another canary and the loop would never terminate). The user decides whether to retry, fix the executor, or use `--exclude=<node>` on a manual resubmit.

6. On timeout: bump walltime 2× and retry the canary **once**. After two timeouts, surface to the user. Track the per-(profile, cluster) timeout count in the run sidecar's `extra` block so a cold-session resume sees the prior attempt count.

7. After a *successful* canary, re-invoke score-submit-plan and proceed to 4c-B. The re-call sees one sample of the same `cmd_sha` and now returns scored candidates.

### 4c-B: `needs_canary: false` (priors exist)

Score the candidates per the **Scoring rubric** in [score-submit-plan.md](../../docs/primitives/score-submit-plan.md) — formula, tie-break, walltime selection, and the empty-quantiles / empty-ETA edge cases all live in the primitive body. Pick the candidate with smallest `total_etc`.

**Adversarial backfill mode** (default-on): `plan-submit` runs in adversarial mode by default. It exploits three orthogonal attack axes against the SLURM backfill scheduler:

1. **Walltime shrink** — recommend p95 × 1.30 from `runtime_prior.elapsed_sec`. Tighter walltime asks fit narrower backfill gaps. Floor 10 min, requires ≥5 prior samples per GPU type.
2. **Footprint shrink** — recommend `--mem` from `peak_host_mem_mb` (p95 × 1.50, ≥10 samples) and `--cpus-per-task` from `cpu_seconds_used / elapsed_sec` (p95 + 1, ≥10 samples). Both axes only **shrink below** the user's defaults — never grow — to avoid silent OOM/cliff kills.
3. **Probe lattice** — sweep `(walltime × mem × constraint)` via `sbatch --test-only` and pick the variant SLURM predicts will start earliest.

Each candidate report carries:

- `recommended_tuple: {constraint, walltime_sec, mem_mb, cpus, predicted_eta_sec, rationale}` — the variant SLURM predicts will start earliest. Rationale is `walltime: ... | mem: ... | cpus: ...` so the user can audit each axis independently.
- `backfill_probes: [...]` — the full lattice with predicted ETAs.

The top-level report also carries two cluster-wide adversarial recommendations when you supply `--target-backfill-window-sec`, `--current-max-array-size`, and `--est-per-task-sec`:

- `array_reshape: {current_max_array_size, recommended_max_array_size, rationale}` — submit smaller, more-numerous arrays so each becomes independently backfillable.
- `walltime_split: {n_segments, segment_walltime_sec, requires_checkpointing, rationale}` — split a long task into chained shorter segments (capped at 8 by default). **`requires_checkpointing: true` means the executor must support resume from checkpoint.** Do NOT auto-apply walltime splitting if the executor isn't checkpoint-aware — every segment boundary kills work.

**Auto-pick rule** (per-candidate): whenever the chosen candidate's `recommended_tuple.predicted_eta_sec is not None`, **automatically use** `recommended_tuple.walltime_sec`, `recommended_tuple.mem_mb`, `recommended_tuple.cpus`, and `recommended_tuple.constraint` for the sbatch invocation in Step 8 — no user prompt. SLURM has confirmed a fitting backfill window exists, so we take it. Surface the `rationale` field in the audit file so the choice is replayable.

**Auto-apply rule** (cluster-wide): apply `array_reshape.recommended_max_array_size` automatically when present. **Do NOT auto-apply `walltime_split`** — confirm with the user that the executor checkpoints before chaining.

Fall back to the original walltime/constraint only when:

1. `recommended_tuple.predicted_eta_sec is None` (every probe failed), **or**
2. `recommended_tuple.rationale` indicates "no usable" prior on all three axes simultaneously.

Pass `--no-adversarial` to `plan-submit` only for debugging or on clusters that throttle `--test-only`.

**Closed-loop calibration**: `plan-submit` automatically reads recent samples for the (profile, cluster) and tunes the walltime safety multiplier:

- The top-level `walltime_drift` field reports `{base_safety_mult, adjusted_safety_mult, rationale}` whenever drift was applied. If the rationale says "loosened", the cluster has been cliff-killing recent jobs and the planner is being more conservative; if "tightened", the planner is being more aggressive because past asks were systematically padded.
- After submission, write a prediction sidecar so post-completion ingestion can validate calibration:

  ```python
  from claude_hpc.forecast.calibration import record_prediction_sidecar
  record_prediction_sidecar(
      experiment_dir=Path("."),
      run_id=run_id,
      predicted_eta_sec=recommended_tuple["predicted_eta_sec"],
      constraint=recommended_tuple["constraint"],
      walltime_sec=recommended_tuple["walltime_sec"],
      mem_mb=recommended_tuple["mem_mb"],
      cpus=recommended_tuple["cpus"],
  )
  ```

  The monitor reads the sidecar back and includes `predicted_eta_sec` + `submitted_at_iso` when calling `runtime_prior.append_sample`. The `house-edge` subcommand then aggregates predicted-vs-actual queue time so you can see whether `--test-only` is finding real backfill windows.

- Standalone diagnostics: `hpc-agent walltime-drift --profile X --cluster Y` and `hpc-agent house-edge --profile X --cluster Y` surface the per-cluster signals without re-running the full planner.

For each chosen candidate's `stressed_nodes`, decide per-node whether to soft-exclude using `co_tenants` context — this is the human-judgment moment that no static threshold captures cleanly, so it stays here in the slash command:

- Co-tenant has been running >12h *and* holds >50% of CPU / mem on the node ⇒ exclude (long-running heavy job; unlikely to clear before our submit completes).
- Co-tenant is recently-started or holds little of the node's resources ⇒ allow.
- Multiple co-tenants on a node with combined high resource share ⇒ exclude.

Build the resulting `--exclude=<node1>,<node2>,...` flag and add it to the sbatch invocation in Step 8.

### 4c-C: planner errors

If the `plan-submit` envelope is `ok: false`, fall back to the static-constraint flow: take the constraint from `clusters.yaml`'s `gpu_constraint`, walltime from `clusters.yaml`'s `constraints.max_walltime`, and proceed without an exclude list. Surface the planner error verbatim so the user knows quality awareness is degraded.

### Audit file

After Step 8 returns job IDs, write the decision to `.hpc/runs/<run_id>.decision.json` so the choice is replayable:

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
    "candidates_considered": [
        {
            "constraint": c["constraint"],
            "eta_sec": c["eta_sec_via_test_only"],
            "walltime_required_sec": ...,    # the p95 you computed
            "p_fail": ...,                   # max p_fail across gpus
            "total_etc_sec": ...,            # the cost score
        }
        for c in data["candidates"]
    ],
    "chosen": {
        "constraint": chosen_constraint,
        "walltime_sec": chosen_walltime,
        "exclude_nodes": chosen_excludes,
        "rationale": claude_free_form_text,  # which signals tipped the choice
    },
    "job_ids": job_ids,
}
Path(".hpc/runs") .mkdir(parents=True, exist_ok=True)
Path(f".hpc/runs/{run_id}.decision.json").write_text(json.dumps(decision, indent=2, sort_keys=True))
```

## Step 5: Confirm Run Plan

**Don't hand-author the summary.** Once Step 6c emits the resolved spec via `build-submit-spec`, render the canonical confirmation via the **`summarize-submit-plan`** primitive — byte-stable framing, magnitude-aware confirm prompt:

```bash
hpc-agent summarize-submit-plan --spec /tmp/submit_spec.json
```

The envelope's `data` carries `{headline, body, confirm_prompt}`. Print `headline` and `body` verbatim, then ask `confirm_prompt`. For multi-job submissions, call the primitive once per spec and concatenate the bodies under one combined header. The primitive flips to a magnitude-warning prompt automatically when `total_tasks > 1000`.

## Step 6: Scaffold (or reuse) `.hpc/tasks.py` and write the per-run sidecar

This is the **central agent-driven moment** that makes claude-hpc different from a generic mapreduce library. Instead of the framework guessing parallelization axes from a YAML schema, the LLM walks the user through writing a small `total()` / `resolve(task_id)` module **once per experiment**, then commits it. From then on, every submission reuses it byte-for-byte.

### Step 6a: Reuse if `.hpc/tasks.py` exists

```python
from pathlib import Path
from claude_hpc import (
    framework_subdir, tasks_path, load_tasks_module, compute_cmd_sha,
)

experiment_dir = Path.cwd()
framework_subdir(experiment_dir)        # mkdir .hpc/, write .hpc/.gitignore
tp = tasks_path(experiment_dir)         # .hpc/tasks.py
```

If `tp.exists()`, the experiment was already scaffolded. **Read it as-is**, never regenerate:

```python
tasks = load_tasks_module(tp)
n = tasks.total()
sample = tasks.resolve(0)               # sanity-check signature
print(f"reusing existing .hpc/tasks.py: total()={n}, resolve(0)={sample}")
```

If the user wants to change the axis, tell them to edit `.hpc/tasks.py` directly and re-run `/submit-hpc`. The framework never overwrites a user-authored file. Skip to Step 6c.

### Step 6b: Scaffold from the canonical example (first submit only)

If `tp.exists()` is False, enter the scaffolding sub-flow:

1. **Read the canonical example.** Resolve `claude_hpc/templates/scaffolds/tasks_example.py` via `_PACKAGE_ROOT / "templates" / "tasks_example.py"` and read it. This is the only `tasks.py` reference the framework ships — top-level `FLAGS: dict[str, list[Flag]]`, eager-materialized `_TASKS = [...]`, with three commented-out usage patterns inline (Cartesian product, chunking by row count, date-window backtest).

2. **Gather context for the draft.** Read the user's executor module(s) (the same `info.path` from Step 1's `discover_executors`) and any `meta.json` at the experiment root for axis hints (parameter names, ranges, chunking intent, date windows). Recent run sidecars under `.hpc/runs/` are also a useful source — they capture the full kwargs dict from any previous `tasks.resolve(i)` materializations.

3. **Walk the user through naming the axes.** Conversational, not template-substitution: the agent re-states the axis from Step 3 ("We're going to fan out over `{model, horizon, seed}` — 12 total tasks, sound right?") and confirms the values. The user can paste a snippet, describe in prose, or point at existing code; the agent translates intent into a list of `{name, values}` dicts and a per-executor flag list.

4. **Generate the file via the `build-tasks-py` primitive — don't hand-author it.** The primitive synthesizes the canonical Pattern 1 (cartesian product) layout from the axes + flags spec and validates the result is syntactically valid Python:

   ```bash
   # Spec file the agent writes from the interview answers:
   cat > /tmp/tasks_spec.json <<EOF
   {
     "axes": [
       {"name": "horizon", "values": [1, 5]},
       {"name": "seed", "values": [42, 1337]}
     ],
     "flags_by_executor": {
       "src.ml_ridge": [
         {"name": "horizon", "type": "int", "default": 1},
         {"name": "seed", "type": "int", "default": 42}
       ]
     }
   }
   EOF
   hpc-agent build-tasks-py --spec /tmp/tasks_spec.json --experiment-dir .
   ```

   The envelope's `data` reports `{path, wrote, n_tasks}`. **Refuses to overwrite** an existing `.hpc/tasks.py` without `--force` so a user's hand-edited Pattern 2 (chunking) or Pattern 3 (date-window) conversion survives a re-submission.

   When the user wants Pattern 2/3, generate the Pattern 1 starting point first, then have them edit `.hpc/tasks.py` to switch — the contract is just `FLAGS / total() / resolve()`, so any pattern that satisfies it is fine. The framework never overwrites a hand-edited file.

   The kwargs returned by `resolve(task_id)` must use names that match the FLAGS list's `flag(name, ...)` entries (with underscores; argparse converts to `--hyphenated` automatically). A typo here surfaces as `argparse: unrecognized arguments` on the cluster — not as a friendly KeyError. The primitive enforces this on generation, but a hand-edit can drift; re-running `build-tasks-py --force` is the canonical fix.

4. **Copy the dispatcher.** Whether or not the experiment is using the new `compute(args)` contract, drop in the framework's static dispatcher so the cluster job script can invoke `python -m cli <executor_module> ...`:

   ```python
   import shutil
   from claude_hpc import _PACKAGE_ROOT
   shutil.copy(
       _PACKAGE_ROOT / "templates" / "cli_dispatcher.py",
       experiment_dir / ".hpc" / "cli.py",
   )
   ```

   This file is one-time scaffolding — never regenerated even when `tasks.py FLAGS` changes (the dispatcher reads FLAGS at runtime).

5. **Commit the generated files.** `build-tasks-py` already wrote `.hpc/tasks.py`; the dispatcher copy in step 4 wrote `.hpc/cli.py`. Just commit:

   ```bash
   git add .hpc/tasks.py .hpc/cli.py
   git commit -m "Scaffold .hpc/tasks.py + cli.py for $EXECUTOR_NAME"
   ```

   Print the commit SHA. **No push** — the user controls when their work goes upstream. If the working tree is detached or the directory is not a git repo, warn the user and continue (the files still get written; commit is best-effort). Subsequent submits hit Step 6a and skip this entire sub-flow.

### Step 6c: Compute `cmd_sha` and check for resume

The materialized task list is the source of identity for the run. Compute the cmd_sha, then check for a prior run via the **`find-prior-run`** primitive (don't grep the runs/ dir by hand):

```python
from claude_hpc import compute_cmd_sha, compute_tasks_py_sha
tasks = load_tasks_module(tp)
cmd_sha = compute_cmd_sha(tasks)        # SHA-256 over normalized resolve(i) dicts
tasks_py_sha = compute_tasks_py_sha(tp)
```

```bash
hpc-agent find-prior-run --experiment-dir . --cmd-sha "$CMD_SHA"
```

The envelope's `data` carries `{found, run_id, is_orphan, status, age_sec, profile, cluster, job_ids, campaign_id, submitted_at}`. Branch on `found` and `is_orphan`:

- `found=False` → fresh submission, continue to Step 6d.
- `found=True, is_orphan=False` → real prior run. **Stop and ask the user**: "I found a prior run with the same cmd_sha: `{run_id}` ({profile} on {cluster}, {age_sec}s ago). Resume (re-dispatch only failed tasks) or fresh (new run_id)?"
- `found=True, is_orphan=True` → half-baked sidecar from a failed batch (no journal job_ids). Don't surface as a resume candidate; offer "Found a half-baked sidecar from a prior failed submit. Run `hpc-agent prune-orphan-sidecars --experiment-dir .` to clean up, then re-submit." or proceed and let `submit_flow_batch`'s auto-prune handle it on the next call.

- **Resume**: call `/monitor-hpc --run-id <prior.stem>` (or `report_status` directly) to enumerate failing task IDs, then build a `ResubmitPlan` via `resubmit_plan(task_count=tasks.total(), failed_task_ids=[...])` and submit via `backend.submit_plan(plan, ...)`. The new sidecar (written below) carries the same `cmd_sha` but a fresh `run_id` — both runs share provenance via the SHA.
- **Fresh**: ask the user how they want the new run distinguished (e.g. a different result_dir suffix, a profile name change, or simply accept that the new sidecar is a deliberate rerun). The new `cmd_sha` will only differ if `tasks.py` itself changes.

### Step 6d: Compute the throughput plan, write the sidecar, build the submit-flow spec

With `total = tasks.total()` known, run Step 4b's throughput planner (already covered above) to get `wave_map`. Two artifacts land here:

1. **The per-run sidecar** at `<experiment>/.hpc/runs/<run_id>.json` — audit trail that the cluster-side dispatcher and the local-side `/monitor-hpc` / `/aggregate-hpc` read.
2. **The submit-flow spec** — the input to `submit-flow`. Use the **`build-submit-spec`** primitive instead of hand-assembling the dict; it synthesizes the `EXECUTOR` / `HPC_RUN_ID` / `HPC_CMD_SHA` / `HPC_TASK_COUNT` / `REPO_DIR` / `MODULES` / `CONDA_SOURCE` / `CONDA_ENV` / `HPC_RUNTIME` / `HPC_CAMPAIGN_ID` keys, picks the canonical script path from `(backend, is_gpu)`, and validates against `schemas/submit_flow.input.json` before returning.

```bash
# After the interview + planner have resolved every field:
hpc-agent build-submit-spec --spec /tmp/resolved.json > /tmp/submit_spec.json
# /tmp/resolved.json is a flat dict of the kwargs the agent collected:
#   profile, cluster, ssh_target, remote_path, run_id, cmd_sha,
#   total_tasks, backend, is_gpu, modules, conda_source, conda_env,
#   runtime, campaign_id, canary, ...  (see build_submit_spec docstring
#   for the full kwargs list).
# The envelope's `data` field is the assembled submit-flow spec —
# drop it into a file and feed it to submit-flow.
```

Per-run sidecar still happens here too:

```python
run_id = f"{profile}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{cmd_sha[:8]}"
git_sha = subprocess.run(
    ["git", "rev-parse", "--short", "HEAD"],
    capture_output=True, text=True,
).stdout.strip() or "nogit"

sidecar_path = write_run_sidecar(
    experiment_dir,
    run_id=run_id,
    cmd_sha=cmd_sha,
    claude_hpc_version=__import__("claude_hpc").__version__,
    submitted_at=datetime.now(timezone.utc).isoformat(),
    executor=run_cmd,                            # full shell cmd. New-contract: "python -m cli src.ml_ridge". Old-contract: "python3 src/ml_ridge.py".
    result_dir_template=result_dir_template,     # e.g. "results/{git_sha}/task_{task_id}"
    task_count=tasks.total(),
    tasks_py_sha=tasks_py_sha,
    wave_map=wave_map,                           # from Step 4b's build_wave_map(plan)
    extra={"git_sha": git_sha},
    # ----- v2 config snapshot — populate everything that applies -----
    cluster=cluster_name,                        # e.g. "hoffman2" / "discovery"
    profile=profile,                             # the label distinguishing this submission shape
    project=project,                             # short project name from the interview
    remote_path=remote_path,
    resources=resources,                         # {"cpus": 8, "mem": "64G", "walltime": "...", ...}
    env=env,                                     # {"modules": "...", "conda_env": "..."}
    env_group=env_group,                         # clusters.yaml env_group key, if used
    constraints=resolved_constraints,            # the per-experiment overlay on clusters.yaml
    gpu_fallback=gpu_fallback,                   # ordered GPU types if applicable
    max_retries=max_retries,
    runtime=runtime,                             # "uv" if requested
    auto_retry=auto_retry,                       # per-category override; None = use defaults
    aggregate_defaults=aggregate_defaults,       # {"require_outputs": "...", "expect_output": "...", "aggregate_cmd": "..."}
)
```

Pass `None` (or omit) for any v2 field that doesn't apply — they're all optional and absent keys are stripped from the on-disk JSON. Subsequent `/aggregate-hpc` and `/monitor-hpc` invocations read these fields back so the user never has to re-answer the interview.

For multi-executor submissions, write one sidecar per executor — `run_id` and `executor` differ, but `tasks.py` is per-experiment and may be shared if the axes match.

`write_run_sidecar` automatically prunes old sidecars past `MAX_RUNS` (default 500; override via `HPC_MAX_RUNS`). Identity is the `run_id`, addressable directly at `.hpc/runs/<run_id>.json`.

**Don't pass `job_ids` here.** The sidecar at this point is a *pending* artifact: ready for the cluster-side dispatcher to read but not yet associated with any qsub'd job. Step 7b–8's `submit-flow` invokes `submit_and_record` after qsub returns, which calls `update_run_sidecar_job_ids` to stamp the freshly-allocated job ids onto this same file. A sidecar that ends the pipeline still missing `job_ids` — i.e. rsync or qsub failed before submit_and_record ran — is the half-baked-sidecar signal `prune_orphan_sidecars` and `submit_flow_batch`'s auto-cleanup key on. You don't need to handle the failure path here; the next submit will silently sweep the orphan.

## Step 6b: Pre-flight Gate

Verify the local environment can submit to `<cluster>` BEFORE any SSH/rsync. Used to live as a standalone `/preflight` command; now folded in here with a per-cluster cache so it only re-checks when stale.

Cache marker: `~/.claude/hpc/<repo_hash>/preflight-<cluster>.json` with `{checked_at, all_ok, cluster}`. TTL 24 h. If the marker exists, `all_ok=true`, and `checked_at` is < 24 h old, log `preflight: cached <N>m ago — OK` and skip to Step 7.

Otherwise, invoke the [check-preflight](../../docs/primitives/check-preflight.md) primitive with `--cluster <name>`. On `data.all_ok == true`: write/update the marker (`checked_at = now()`), continue to Step 7.

On any check failure: do NOT write the marker, do NOT proceed to Step 7. Surface the failing checks with their `detail` fields verbatim (don't paraphrase — the user needs the raw error to fix it). Standard remediations to suggest:

| Failed check | Remediation |
|---|---|
| `ssh_auth_sock` | `ssh-add ~/.ssh/<key>`; check tmux/screen `SSH_AUTH_SOCK` forwarding |
| `ssh_on_path` / `rsync_on_path` | install via system package manager |
| `clusters_yaml_parses` | fix the YAML parse error first; nothing else will work |
| `cluster_known` | typo in `--cluster` vs. `clusters.yaml` entry |
| `cluster_tcp_22` | cluster offline or hostname wrong; do NOT retry blindly |

The sidecar from Step 6 is already on disk, so re-running `/submit-hpc` after the user fixes the env will land in Setup priority 1 ("Previous run") and offer "Resubmit same" — they don't have to re-do the interview.

The user can still invoke `/preflight --cluster <name>` standalone (e.g., to ad-hoc verify SSH agent forwarding without a pending submission); that command writes the same marker, so a recent standalone run satisfies this gate too.

## Step 6c: Pre-submit campaign validation

Before any rsync/qsub, run the `validate-campaign` workflow primitive against the resolved spec. Catches three bug classes that otherwise surface hours later in the queue: fabricated kwargs (executor's `Literal["a","b"]` parameter receiving `"x"`), NaN-trap row references in the input dataset, and walltime / GPU mismatches against historical priors + `.hpc/playbook.yaml` known-bad combinations.

Build the spec from what the interview + planner have already resolved:

```python
from claude_hpc._schema_models.workflows.validate_campaign import ValidateCampaignSpec

vc_spec = ValidateCampaignSpec(
    profile=profile,
    cluster=cluster,
    executor_module="src.train",        # only when known; otherwise None
    executor_function="main",
    dataset_path=interview_intent.get("dataset_path"),
    dataset_loader=interview_intent.get("dataset_loader"),
    dataset_row_indices=interview_intent.get("dataset_row_indices"),
    dataset_required_non_null_cols=interview_intent.get(
        "dataset_required_non_null_cols", []
    ),
    requested_walltime_sec=resources["walltime_sec"],
    gpu_type=resources.get("gpu_type"),
    workload_tags=interview_intent.get("workload_tags", []),
)
```

Invoke:

```bash
python -m claude_hpc validate-campaign --spec validate_campaign.input.json --experiment-dir .
```

Branch on `data.overall`:

- `pass` — proceed to Step 7.
- `warn` — surface every warning to the user; proceed to Step 7 unless the user explicitly asks to fix something first.
- `fail` — do NOT proceed. List each `error`-severity finding with its `code`, `message`, and `suggested_fix`. Apply fixes, re-run `/validate-campaign`, repeat until `pass` or `warn`. There is no `--force` flag by design — if a rule is wrong for the project, edit `.hpc/playbook.yaml` (one version-controlled commit) rather than override at runtime.

The validator is fast (no SSH, no qsub) and idempotent. Skipping a validator is automatic: if `executor_module` is None the signature check is skipped, if `dataset_path` is None the dataset check is skipped, etc. The slash command can construct a partial spec and the rest auto-skips.

## Step 6d: Predict start time + recommend submit-at-T offset

Before qsub, run the [predict-start-time](../../docs/primitives/predict-start-time.md) primitive to forecast when this job will actually start. The primitive sweeps candidate submit-at-T offsets and returns whichever minimizes total time-to-actual-start (offset + predicted wait after submission).

Two things drive the prediction:

1. **Two simulators** compute deterministic floors from the current squeue snapshot — pessimistic (pure FIFO drain, hard lower bound) and optimistic (phantom-slot backfill, loose upper bound on backfill leverage).
2. **A LightGBM residual model** learns the empirical overhead between the FIFO floor and observed start times across historical jobs. Captures stochastic reality (future arrivals, fair-share decay, scheduler config drift) the simulator can't model. When no model has been trained yet, the predictor falls back to the pessimistic floor + zero overhead.

Inputs the slash command must gather (over SSH, before invoking the primitive):

```bash
SQUEUE=$(ssh "$SSH_TARGET" "squeue --user='*' -O 'JOBID|PRIORITY|PARTITION|USERNAME|STATE|TIME_LEFT|TIME_LIMIT'")
SSHARE=$(ssh "$SSH_TARGET" "sshare -P")  # cached via forecast/fairshare_cache for ~1h
```

Build the spec:

```json
{
  "now_iso": "<UTC ISO>",
  "squeue_text": "<above>",
  "sshare_text": "<above>",
  "partition": "<from cluster config>",
  "partition_slot_count": <from `scontrol show partition`>,
  "your_priority": <estimated from sprio of similar past jobs, or fallback to median>,
  "your_walltime_sec": <Step 4b walltime decision>,
  "your_user": "$USER",
  "your_constraint": "<resolved Features= expression>",
  "candidate_offsets_hours": [0, 1, 3, 6, 12, 24],
  "model_path": ".hpc/wait_predictor"
}
```

Invoke:

```bash
python -m claude_hpc predict-start-time --spec spec.json --experiment-dir .
```

Surface the result:

- `data.best_submit_offset_hours == 0` → **submit now** is the lowest-total-time option. Continue to Step 7.
- `data.best_submit_offset_hours > 0` → **wait N hours, then submit**. Show the user: "Predicted total time to actual start: 45 min (submit now would be 4h). OK to wait?" — if they decline, proceed anyway; if they accept, schedule the submit (or pause and let the operator resume manually).
- The full sweep is in `data.candidates` for transparency. When uncertainty fields are populated (`predicted_iso_p10` / `predicted_iso_p90` on each candidate), surface them as "expected 45min, worst-case 4h" rather than a point estimate.

This step is advisory, NOT a gate. The agent always proceeds to Step 7 (or follows the user's decision); the predictor is decision support, not refusal logic.

If you don't have squeue / sshare snapshots history yet (cold cluster, first time using the predictor), the predictor returns `method="floor_only"` and the prediction equals the pessimistic floor. Snapshotting + training catches up over the next ~1-2 weeks; until then the floor is still actionable.

## Step 7: Sync to Cluster

Two pipes populate the cluster's `$REMOTE_PATH`. **Don't hand-copy any framework files** — `deploy_runtime` does that via scp, and rsync would otherwise overwrite the cluster-side `.hpc/_hpc_dispatch.py` etc. with files that don't exist locally.

1. **`rsync_push`** ships your code plus the local `.hpc/` (which contains only `tasks.py` and `runs/<run_id>.json` — no framework files):

   ```bash
   rsync -az --delete \
       --exclude='.git/' --exclude='__pycache__/' --exclude='*.pyc' \
       --exclude='claude_hpc/' \
       --exclude='.hpc/_hpc_dispatch.py' \
       --exclude='.hpc/_hpc_combiner.py' \
       --exclude='.hpc/templates/' \
       # ... plus any project-specific excludes ...
       . $SSH_TARGET:$REMOTE_PATH/
   ```

   The `.hpc/_hpc_*.py` and `.hpc/templates/` excludes prevent `--delete` from wiping the framework files that `deploy_runtime` placed on the cluster. `DEFAULT_RSYNC_EXCLUDES` in `claude_hpc.infra.remote` has these baked in; if you call `rsync_push` directly, you get them for free.

2. **`deploy_runtime`** scp's the framework files into `{remote_path}/.hpc/`:
   - `_hpc_dispatch.py` (the framework executor)
   - `_hpc_combiner.py`
   - `templates/{cpu_array,gpu_array}.{sh,slurm}`
   - and the importable stubs `claude_hpc/map/{context,metrics_io}.py` (these go to `{remote_path}/claude_hpc/map/`, not `.hpc/`)

   ```python
   from claude_hpc import deploy_runtime
   deploy_runtime(ssh_target=cluster.ssh_target, remote_path=remote_path)
   ```

   Run **after** `rsync_push` (rsync's `--delete` would otherwise blow away the freshly-scp'd files; the excludes above protect them, but ordering remains important on every submit).

Verify deployment — existence check (paths are now under `.hpc/`):
```bash
ssh $SSH_TARGET 'ls '"$REMOTE_PATH"'/.hpc/tasks.py '"$REMOTE_PATH"'/.hpc/runs/<run_id>.json '"$REMOTE_PATH"'/.hpc/_hpc_dispatch.py'
```

**Verify content, not just existence.** `rsync` exit 0 is necessary but not sufficient: a WSL/DNS hiccup or stale SSH config can cause rsync to silently transfer nothing while still returning success. Before submitting a full array, spot-check the hash of 2–3 files that *should* have just changed (e.g., a source file and `tasks.py`):

```bash
# Local hashes
md5sum .hpc/tasks.py src/<changed_file>.py
# Remote hashes
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && md5sum .hpc/tasks.py src/<changed_file>.py'
```

If any hash differs, STOP — re-run rsync with verbose flags (`-avz`) and investigate DNS/ssh-config issues before submitting.

## Step 7b–8: Invoke `submit-flow` (workflow atom)

Steps 7 (rsync), 7b (canary), 8 (qsub), and 10 (record) are **one CLI call** to the workflow atom `hpc-agent submit-flow`. The slash command's job is to assemble the spec from the inputs collected in Steps 1–6 and invoke. The atom does pre-flight + rsync + deploy + optional canary + qsub + sidecar/journal write, returning a single JSON envelope.

Spec shape (matches `schemas/submit_flow.input.json`):

```json
{
  "profile": "<job_name>",
  "cluster": "<cluster_name>",
  "ssh_target": "user@host",
  "remote_path": "<remote_path>",
  "job_name": "<job_name>",
  "run_id": "<run_id from Step 6d>",
  "total_tasks": <tasks.total()>,
  "backend": "sge_remote",
  "script": ".hpc/templates/cpu_array.sh",
  "job_env": {
    "EXECUTOR": "python3 .hpc/_hpc_dispatch.py",
    "HPC_RUN_ID": "<run_id>",
    "HPC_CMD_SHA": "<cmd_sha>",
    "HPC_TASK_COUNT": "<total_tasks>",
    "REPO_DIR": "<remote_path>",
    "MODULES": "<detected modules>",
    "CONDA_SOURCE": "<cluster.conda_source>",
    "CONDA_ENV": "<detected conda_env>"
  },
  "pass_env_keys": ["EXECUTOR","HPC_RUN_ID","HPC_CMD_SHA","HPC_TASK_COUNT","REPO_DIR","MODULES","CONDA_SOURCE","CONDA_ENV"],
  "canary": true,
  "campaign_id": "<slug>",
  "runtime": "uv",
  "skip_preflight": true
}
```

`skip_preflight: true` is correct here because Step 6b's pre-flight gate just ran. The atom honors it to avoid a duplicate SSH probe.

The cluster-side template translates the scheduler's per-task index (`SGE_TASK_ID` / `SLURM_ARRAY_TASK_ID`) into `HPC_TASK_ID` (0-based) before exec'ing `$EXECUTOR`, which then imports `.hpc/tasks.py`, calls `tasks.resolve(HPC_TASK_ID)`, and runs the executor command from the sidecar with kwargs merged into the env.

For GPU jobs: pick `script: ".hpc/templates/gpu_array.sh"` (SGE) or `gpu_array.slurm` (SLURM). The atom doesn't infer this — caller picks based on detected resources.

Invoke:

```bash
hpc-agent submit-flow --spec spec.json --experiment-dir .
```

Parse the envelope:
- `data.deduped: true` — a journal record for this `run_id` already exists. The original cluster jobs are running. Do NOT re-invoke. Switch to `/monitor-hpc <run_id>`.
- `data.deduped: false` — fresh submission. Capture `data.run_id`, `data.job_ids`, and `data.canary_job_ids` (when `canary_done`). Continue to Step 8b.

On error envelopes, branch by `error_code` per `submit-flow`'s contract (`ssh_unreachable`, `remote_command_failed`, `spec_invalid`).

To opt out of the canary (already smoke-tested or single-task submission), set `"canary": false` in the spec — the slash command's `--no-canary` flag from Step 2 maps directly here.

**Multi-executor / multi-spec submissions**: write the spec as `{"specs": [<per-spec dict>, ...], "rsync_excludes": [...], "skip_preflight": ...}` (each entry under `specs` matches the per-spec shape above). `submit-flow` detects the batch shape and auto-routes to the bundled path — same `hpc-agent submit-flow --spec X --experiment-dir .` call. Entries MUST share `(ssh_target, remote_path)`; mixed-cluster batches raise `spec_invalid` (split by target and call once per group). The envelope wraps `{"results": [<per-spec submit-flow envelope>, ...], "n_results": N}`; parse each entry with the same dedup/error logic.

**Note on canary semantics:** `submit-flow`'s canary is a smoke test of the submission machinery (qsub accepts the spec; scheduler returns a job ID). It does NOT wait for canary completion or verify outputs — that elaborate "wait for terminal + grep logs + check artifacts" protocol stays here in the slash command (see "Canary verification" below) for the human-interactive path. Higher-level workflows like `/campaign-hpc` rely on the lighter check.

### Canary verification (route through `verify-canary`)

When `data.canary_done: true`, **don't hand-author the wait + grep + output-check protocol** — call the `verify-canary` workflow atom, which polls the canary to terminal, scans stderr for known failure markers, and (optionally) verifies an expected output artifact:

```bash
hpc-agent verify-canary \
    --experiment-dir . \
    --canary-run-id "$CANARY_RUN_ID" \
    --expect-output "results/seed_42/metrics.json"   # optional
```

The envelope's `data` carries `{ok, failure_kind, details, stderr_tail}`. Branch:

- `ok=True` → continue to the main array submit. The atom already verified exit code 0, no failure markers (`[dispatch] FAILED` / `ImportError` / `ModuleNotFoundError` / `Traceback` / `Out of memory` / `Segmentation fault`), and the expected output (if any).
- `ok=False` → surface `stderr_tail` to the user **verbatim** (don't paraphrase — they need the raw error to fix it). `failure_kind` tags the category (`dispatcher_failed` / `import_error` / `oom_killed` / `missing_output` / `timeout` / etc.) so you can frame the user-facing message but the diagnosis came from the primitive, not the agent.

If verification fails, do NOT report Step 9 success. The fix cost is 1 task; skipping verification and discovering a bad pipeline after 5000 tasks wastes hours of cluster time.

## Step 8b: Verify the array is actually queued/running

`qsub`/`sbatch` returning a job ID is necessary but not sufficient — the scheduler can still place the array into an error state (`Eqw` on SGE, `BOOT_FAIL`/`NODE_FAIL` on SLURM) or, on a wedged controller, drop the registration entirely. Confirm each returned job ID is alive on the cluster **before** reporting success (Step 9) or writing the journal record (Step 10). A poisoned run that lands in the journal is worse than a clean failure here, because `/monitor-hpc` will keep latching onto a dead job ID.

Query the scheduler for every job ID returned by `backend.submit_array` / `backend.submit_plan`:

```bash
# SLURM
ssh $SSH_TARGET 'squeue -j '"$JOB_IDS"' -h -o "%i %T %r"; \
                 sacct -j '"$JOB_IDS"' -n -P -o JobID,State,Reason 2>&1 | head'

# SGE — qstat -j prints the queue-instance reason if the job is in error
ssh $SSH_TARGET 'qstat -j '"$JOB_IDS"' 2>&1 | head -40; \
                 qstat -u '"$USER"' | awk "NR>2"'
```

`$JOB_IDS` is comma-separated for SLURM (`12345,12346`) and space-separated for SGE.

**Healthy** (proceed): `PENDING` / `RUNNING` / `CONFIGURING` / `COMPLETING` (SLURM); `qw` / `hqw` / `r` / `t` / `Rq` / `Rr` (SGE). Wave-2+ jobs from a plan-based submission are *expected* to be `PENDING` with `Reason=Dependency` (SLURM) or `hqw` (SGE) — that is healthy, not a failure.

**Failed** (abort — do NOT call `submit_and_record`):
- SLURM state in `{BOOT_FAIL, FAILED, NODE_FAIL, OUT_OF_MEMORY, TIMEOUT, DEADLINE, REVOKED, SPECIAL_EXIT}`, or `CANCELLED` within seconds of submit
- SGE state starting with `E` (e.g. `Eqw`) or `d` (deletion in progress)
- Job ID absent from both `squeue`/`qstat` and `sacct`/`qacct` after one retry (~3s pause): the scheduler never registered it

If the first query shows an ID as unknown, retry **once** after a brief pause (busy SLURM controllers can lag a second or two before `squeue` reflects a new submission). If still unknown, treat as failed.

On failure: surface the scheduler's reason verbatim (`qstat -j <id>` line `error reason 1:` for SGE, `sacct -j <id> -o JobID,State,Reason` for SLURM), tell the user which job ID is bad, and stop. Do not run Step 9 or Step 10 — the partial state is recoverable only if nothing was journaled. The user can then either fix the underlying issue (resources, queue, env) and re-run `/submit-hpc`, or, for SGE-specific transient `Eqw`, run `qmod -cj <jobid>` and re-verify.

If the canary in Step 7b just succeeded, this verification almost always passes; the value is catching the rare case where the full-array submit hits a quota/AR/queue limit the canary did not.

## Step 9: Cache and Report

### Cache decisions
Save to Claude Code memory for this project:
- Executor directory, cluster, remote_path
- Environment: modules, conda_env per executor type (CPU/GPU)
- Default resources

### Report
After submission **and the Step 8b verification**:
1. Parse the job ID from submission output
2. Report: job ID, executor(s), grid dimensions, total tasks, cluster, and the verified scheduler state (e.g. "all 4 array jobs PENDING/RUNNING")
3. Suggest running `/monitor-hpc` to track progress

## Step 10: (folded into Step 7b–8)

The journal write happens inside `submit-flow` via `runner.submit_and_record`. Nothing additional to do here. For multi-executor submissions (one sidecar per executor), invoke `submit-flow` once per submitted job — each call writes its own sidecar.

## Common Failure Modes

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Eqw` state (SGE) | Job error | `qmod -cj <JOBID>` or resubmit |
| `PENDING` (SLURM) for >30min | Resource unavailable | Check `sinfo`, try different partition |
| Memory exceeded | Exceeded mem limit | Resubmit with higher memory |
| Walltime exceeded | Exceeded time limit | Resubmit with longer walltime |
| ModuleNotFoundError | Env not set up | Check modules and conda_env |
| rsync failure | SSH key issue | Check `ssh $SSH_TARGET hostname` first |
| `--features` not recognized | Executor doesn't support that arg | Check `--help`, update executor |
