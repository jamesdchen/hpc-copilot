# End-to-end submit sequence

What happens between a user typing `/submit-hpc` and their results
landing in `aggregated.json`. This walkthrough traces the full
pipeline so you can see how the layers interact in practice.

## The high-level shape

```
User chat                   The slash               The skill              The bare worker            The cluster
─────────                   ─────────                ──────────              ───────────────            ───────────
                                                                                                    
/submit-hpc ──→ slash body                                                                          
                  parses $ARGUMENTS                                                                
                  builds initial spec                                                              
                  ─→ Skill(hpc-submit, spec)  ──→ skill body                                       
                                                     load-context                                  
                                                     resolve fields                                
                                                     compose sub-skills                            
                                                     ─→ Bash hpc-agent run --workflow submit ──→ bare worker 
                                                                                       reads worker_prompts/submit.md
                                                                                       execute deterministic sequence:
                                                                                          export-package
                                                                                          rsync to cluster                    
                                                                                          qsub array job  ──────────────→ scheduler queues
                                                                                          verify-canary                       canary runs
                                                                                          write sidecar                       array runs
                                                                                          return envelope ←─────────────── tasks complete
                                                     ←── final envelope                                                    
                                  ←── final envelope                                                
        ←── result surfaced                                                                        
            to user                                                                                
```

Sequence below walks through each step in more detail.

## Step 0: User intent

The user types something like:

```
/submit-hpc run ridge with horizon=[1, 5, 25]
```

Or invokes a workflow that calls into `/submit-hpc` (a campaign tick,
for example).

## Step 1: Slash body executes (interview layer)

The slash command body (`src/slash_commands/commands/submit-hpc.md`)
is loaded into the chat agent's context. It does:

1. **Parse `$ARGUMENTS`** into an initial spec dict. The user said
   "run ridge with horizon=[1, 5, 25]", so the slash extracts
   `task_generator: {kind: "cartesian_product", params: {model: ["ridge"], horizon: [1, 5, 25]}}`.

2. **Invoke the `hpc-submit` skill** via the Skill tool with the
   initial spec:

   ```
   Skill("hpc-submit", {
     experiment_dir: ".",
     task_generator: {...},
     # other fields the user pre-stated, if any
   })
   ```

   The skill body is inlined into the chat agent's context. The agent
   now executes the skill's procedure.

## Step 2: Workflow skill executes (decision layer)

The `hpc-submit` skill body
(`src/slash_commands/skills/hpc-submit/SKILL.md`) walks every
resolution step.

### Step 2.1: Load context

```bash
hpc-agent load-context --experiment-dir .
```

Returns `{action, run_id, candidates, next_step_hint, in_flight,
campaigns}` based on on-disk state. Skill branches on `action`:

- `monitor` (in-flight run exists) → return `spec_invalid:
  already_in_flight`; caller handles.
- `reuse` (recent sidecars exist) → can shortcut to reusing the prior
  spec.
- `interview` (tasks.py exists, no run history) → use existing tasks.py.
- `fresh` (nothing exists) → full new-experiment flow.

### Step 2.2: Resolve each axis

For each field the skill needs to fill in:

- **Cluster**: caller supplied? Use. Otherwise read `clusters.yaml`. Single cluster → use; multiple → add to ambiguities.
- **Entry point**: `@register_run` on disk? Resolve. `interview.json` exists? Honor. Neither? Compose `hpc-wrap-entry-point` sub-skill.
- **DataAxis**: cache hit in `axes.yaml`? Use. Otherwise compose `hpc-classify-axis` sub-skill → which itself calls `classify-axis-easy` (the matcher) → which usually commits autonomously.
- **Homogeneous axes**: cache hit in `axes.yaml`? Use. Otherwise compose `hpc-build-executor` sub-skill (axes-init companion).
- **Walltime / GPU / partition**: auto-resolve from runtime priors via `read-runtime-prior` (optional plugin, if installed) or cluster defaults.

If any field can't auto-resolve, add to `ambiguities` list. Continue
walking — don't early-return.

### Step 2.3: Handle ambiguities

If `ambiguities` is non-empty:

```json
{
  "ok": false,
  "error_code": "needs_resolution",
  "data": {
    "resolved": {...},
    "ambiguities": [...]
  }
}
```

The skill returns this envelope to the slash. The slash walks dialogs
with the user for each ambiguity (in topo-sorted dependency order),
collects answers, re-invokes the skill with an augmented spec.
Bounded by DAG depth (~3 rounds max).

For autonomous callers (no slash), the caller applies each
ambiguity's `safe_default` and re-invokes.

### Step 2.4: Assemble final fields

When ambiguities are empty, the skill assembles the final spec:

```json
{
  "experiment_dir": "/path/to/exp",
  "cluster": "hoffman2",
  "profile": "ridge",
  "data_axis": {...},
  "homogeneous_axes": [...],
  "task_generator": {...},
  "walltime_sec": 7200,
  "gpu_type": "a100",
  "no_canary": false,
  "campaign_id": null
}
```

### Step 2.5: Hand off to the worker

```bash
hpc-agent run --workflow submit --fields-json '<fields>'
```

This is the execution boundary. The skill's job ends here; everything
below runs in a fresh-context `claude -p --bare` worker.

## Step 3: Bare worker executes (execution layer)

`hpc-agent run --workflow submit` spawns a `claude -p --bare` worker.
`spawn_prompt._procedure_body` inlines the contents of
`src/hpc_agent/_kernel/extension/worker_prompts/submit.md` into the
worker's `cacheable_prefix`. The worker's context contains the worker
prompt + the resolved fields spec.

The worker executes the deterministic sequence (no decisions about
the experiment's content; only plumbing decisions about workflow state):

### Step 3.1: Suggest setup action

```bash
hpc-agent suggest-setup-action --experiment-dir .
```

Returns the same `action` the skill saw, but used here for branching
inside the worker (not for re-invocation).

### Step 3.2: Build src/ package

```bash
hpc-agent export-package --experiment-dir .
```

Builds `src/` from notebooks. Content-hash cached against
`.build-cache.json` — no-op when nothing changed. The cluster never
builds; the built `src/` rides the deploy rsync.

### Step 3.3: Run pre-flight gate (Step 6b)

Reads `preflight_<cluster>.json`. If fresh (<24h), skip. Otherwise
run `check-preflight`. If the cluster isn't reachable, abort with
`ssh_unreachable`.

### Step 3.4: Validate the campaign

```bash
hpc-agent validate-campaign --spec spec.json --experiment-dir .
```

Catches fabricated kwargs, NaN-trap row references, walltime/GPU
mismatches. Errors block; warnings proceed with a note.

### Step 3.5: (Optional, plugin) Predict start time

```bash
hpc-agent predict-start-time --spec ...
```

Returns a predicted start time + the best-submit-window offset. The
worker may decide to wait or proceed.

### Step 3.6: Build submit spec

```bash
hpc-agent build-submit-spec --interview-json interview.json ...
```

Turns the resolved fields into a validated `submit_flow.input.json`.

### Step 3.7: Submit-flow

```bash
hpc-agent submit-flow --spec submit_flow.input.json
```

The big one. Does:

1. **Rsync** the experiment directory (including built `src/`) to cluster scratch.
2. **Deploy** scaffolding (`.hpc/cli.py` dispatcher, `tasks.py` per-task lookup).
3. **qsub** the array job (SLURM `sbatch --array=0-N` or SGE `qsub -t 1-N`).
4. **Record sidecar** at `~/.claude/hpc/<repo>/runs/<run_id>.json` with the new run's metadata.
5. **Append journal entry** to `journal.jsonl`.

The submit-flow primitive is idempotent on `cmd_sha` — a replay
returns `deduped: true` and emits no cluster-side side effects.

### Step 3.8: Verify canary

If `no_canary` is false (default), the worker runs:

```bash
hpc-agent verify-canary --run-id <run_id>
```

Submits one task (`task_id=0`) ahead of the array; waits for it;
fails fast if it errors. Catches argparse mismatches, missing
modules, environment issues before fanning out to N tasks.

### Step 3.9: Verify scheduler accepted the array

```bash
hpc-agent monitor-summary --run-id <run_id>
```

Confirms the scheduler queued the array and the canary completed.
Returns the lifecycle state.

### Step 3.10: Return envelope to the skill

The worker prints a JSON envelope to stdout:

```json
{
  "ok": true,
  "data": {
    "report": {
      "result": {
        "run_id": "ml_ridge_20260101_120000_abcd1234",
        "job_ids": ["1234567"],
        "scheduler_state": "queued",
        "complete_count": 0,
        "total_tasks": 100
      },
      "decisions": [
        {"step": "verify-canary", "outcome": "passed", "...": "..."},
        ...
      ],
      "anomalies": []
    }
  }
}
```

## Step 4: Skill returns to the slash

The skill receives the worker's envelope and surfaces it to its caller
(the slash). The skill itself doesn't transform the envelope; it just
propagates.

## Step 5: Slash surfaces to the user

The slash reports back:

```
Submitted run ml_ridge_20260101_120000_abcd1234.

Job IDs: 1234567 (array=0-99)
Scheduler state: queued
Canary: passed in 22s

Submit complete. To monitor: /monitor-hpc
```

## Step 6: Time passes — the array runs on cluster

The user can disconnect; the cluster runs the array. Each task:

1. Receives `SLURM_ARRAY_TASK_ID` (or SGE equivalent)
2. Loads `tasks.py`, calls `resolve(task_id)` to get kwargs
3. Calls the user's `@register_run` function with those kwargs
4. Writes per-task metrics sidecar to scratch
5. Exits

When a wave's tasks complete, the framework's per-wave combiner runs
as a dependent job (added during submit-flow's qsub step).

## Step 7: User monitors

The user types `/monitor-hpc` (or schedules it via cron). Same
slash → skill → bare worker pattern, but now the worker is reading
sidecars + scheduler state instead of submitting. The worker reports
lifecycle state.

If the run completes with failures (>0 failed tasks), the worker
applies the resubmit policy (<10% → auto-resubmit; >10% → escalate to
the user).

## Step 8: User aggregates

When the run reaches `lifecycle_state: complete`, the user (or the
campaign driver) invokes `/aggregate-hpc`. Same pattern. The
aggregate worker:

1. `verify-aggregation-complete --run-id <id>` — confirms all waves done
2. `rsync-pull _combiner/` from cluster scratch → `<experiment>/runs/<id>/`
3. Runs the reducer (`models/mapreduce/reduce/`) to produce
   `aggregated.json`
4. Ingests runtime samples into `runtime_priors/` for future
   walltime predictions
5. Updates the sidecar's `lifecycle_state` to `aggregated`

The aggregate worker returns the final metrics envelope to the slash;
the slash surfaces to the user.

## Variants

### Campaign tick

A `/campaign-hpc` invocation:

- Slash collects path (A or B) + slug on the first tick; subsequent
  ticks reuse them
- Invokes `Skill("hpc-campaign", {...})`
- The skill composes `hpc-submit` → `hpc-status` → `hpc-aggregate`
  per iteration
- Records the iteration into the campaign cursor
- Returns the tick's result to the slash
- User kicks the next tick when ready (or cron does)

### Resubmit

A resubmit doesn't go through `/submit-hpc`. It's invoked from within
the `monitor-flow` worker when `<10%` failure rate is detected:

```bash
hpc-agent resubmit --run-id <id> --task-ids <failed-list>
```

Adds a resubmit wave to the same run (same `run_id`); no new sidecar.

### Autonomous caller (MARs)

An autonomous agent doesn't type `/submit-hpc`. It invokes:

```typescript
Skill("hpc-submit", { experiment_dir, task_generator, ... })
```

directly via the inherited Skill tool. The skill resolves
ambiguities autonomously (applying `safe_default` per entry), or
returns `needs_resolution` for fields the caller is responsible for
(like `task_generator` itself, which the agent had to declare in the
first place).

The autonomous caller handles the resolution loop in its own code,
re-invoking the skill with augmented specs until it gets a final
envelope.

## What's NOT in the sequence

A few things sometimes confused with this flow:

- **The DataAxis classification is mostly a no-op.** Most experiments
  are `Independent` at the inner level; the matcher commits autonomously;
  the slash never sees a data_axis dialog. The dialog exists only for
  unusual experiments (stencil PDEs, etc.).
- **The wave_map is computed inside submit-flow's planner.** It's not
  exposed to the user or the skill; it's an internal implementation
  detail of `submit-flow` choosing how to chunk the array.
- **The stage DAG is trivial for single-stage experiments.** Most
  experiments have one `@register_run` function and one stage; the
  framework's stage machinery is invisible in that case.
- **The campaign integration only activates when `campaign_id` is set.**
  Without a campaign, each `/submit-hpc` is standalone.

## See also

- [`parallelization-axes.md`](parallelization-axes.md) — the five axes used during submission
- [`state-model.md`](state-model.md) — what state files this sequence reads and writes
- [`skill-policy.md`](skill-policy.md) — the layered architecture (interview / decision / execution)
- [`campaign-lifecycle.md`](campaign-lifecycle.md) — campaign-specific extensions to this sequence
