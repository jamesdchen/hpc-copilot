`/hpc-axes-init` is the **human-facing wrapper** around the `hpc-build-executor` skill (covering its axes-init companion half).

The skill itself is agent-autonomous — it inspects `tasks.py`, applies homogeneity heuristics, and invokes `hpc-agent axes-init` without `[Y/n]` prompts (a MARs experiment agent calls it with `--name` + `--output-dir` + `--homogeneous-axes` and lets the heuristics decide). This slash command sits *between* the user and the skill: it conducts the propose-then-confirm dialog over the axes classification, then invokes the skill with the user-confirmed `homogeneous_axes` baked into the spec.

This slash command is the human-facing entry point for the **axes-init** half — initializing `.hpc/axes.yaml` so the framework can pick a parallelism axis automatically at submit time. Reasons to invoke standalone (rather than letting `/submit-hpc` walk through it):

- The experiment already has `tasks.py` but no `axes.yaml` (cold-start case before first submission).
- The experimenter wants to pre-declare axis classifications without committing to a submission yet.
- The parallelism shape changed (axis added, semantics flipped) and the existing `axes.yaml` needs replacing — pass `--force` after the user confirms.

## Procedure (in-chat agent)

### 1. Read tasks.py and enumerate the parallel axes

Inspect the experiment for parallel axes. Common shapes: a `resolve(task_id)` function returning kwargs derived from `task_id` via cartesian product over named lists; a grid-search dict the executor reads; an explicit per-axis loop in driver code. For each named dimension, count its cardinality.

### 2. Walk the homogeneity heuristic per axis

Classify each axis using the experiment's semantics. The heuristic that often holds:

- Replicates / seeds / folds / cross-validation windows / time-series backtest windows → typically **homogeneous** (same compute on slightly different data).
- Model class / architecture / algorithm → typically **heterogeneous** (orders-of-magnitude different cost).
- Data type / dataset → depends on dataset sizes; usually mildly heterogeneous.
- Hyperparameter sweeps → depends; learning rates rarely change cost; layer counts usually do.

### 3. Propose, then confirm

Show the experimenter the proposal with one-sentence reasoning per axis:

```
Found these parallel axes in your experiment:
 • `window` (20 values) — homogeneous (same model trained on a 6-month rolling window)
 • `model` (4 values) — heterogeneous (linear / ridge / xgboost / neural_net have very different runtimes)
 • `data_type` (3 values) — heterogeneous (equities are 10x larger than fx)

I'll write `.hpc/axes.yaml` with `homogeneous_axes: [window]` so the framework promotes `window` to the task array.

Looks right? [Y/n]
```

On **N**, abort without writing. The user can re-run `/hpc-axes-init` later, or write `.hpc/axes.yaml` by hand.

### 4. Invoke the `hpc-build-executor` skill with the resolved `homogeneous_axes`

On **Y**, invoke the `hpc-build-executor` skill via the Skill tool (`skills/hpc-build-executor/SKILL.md`) with the user-confirmed `homogeneous_axes` list. The skill, seeing a complete spec, skips its own heuristic (Step 2 of the axes-init companion) and invokes `hpc-agent axes-init --homogeneous-axes <list>`.

**If `axes.yaml` already exists**, the primitive returns `wrote: false`. Re-prompt the user asking whether to pass `--force` (they may have hand-edited the file). Don't auto-force.

## Notes

- **The picker doesn't require this file to function** — when no `axes.yaml` exists and no priors exist, the picker returns `(None, "no axes.yaml")` and the caller falls back to asking the user explicitly. Running `/hpc-axes-init` makes the cold-start path *automatic* instead of interactive.
- **Field-mirror discipline**: the schema permits exactly the fields the framework can act on. Putting search-space definitions or objective functions here is rejected at validation time. Keep that intent in `tasks.py` / executor code where it belongs.
- **Why this slash exists.** The skill is autonomous (it applies the same heuristic without confirmation). The slash exists so the human's domain knowledge — *which* axes are actually homogeneous in this experiment, e.g. when "seed" is misleadingly named but actually swaps the model — overrides the autonomous heuristic before `axes.yaml` is written.
