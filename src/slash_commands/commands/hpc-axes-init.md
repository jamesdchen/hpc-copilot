Initialize the per-experiment axes config so the framework can pick a parallelism axis automatically at submit time.

This skill composes the [axes-init](../../docs/primitives/axes-init.md) primitive — see that file for full input/output contract. The slash command is the **interactive** wrapper; the same primitive is callable headlessly via `hpc-mapreduce axes-init` for non-Claude-Code agents (e.g. MARs).

## Why this exists

Clusters privilege exactly one axis of parallelism: the task array. When an experiment has more than one parallel dimension (e.g. `models × data_types × backtest_windows`), the framework needs to know which one to promote to the array. The signal is **per-axis runtime homogeneity**: tasks within a task array share walltime + memory reservation, so heterogeneity within the array forces over-provisioning to the worst-case task. The most homogeneous axis is the right one to put on the array.

When runtime priors exist, the picker uses observed coefficient-of-variation (warm path). When they don't, it falls back to this file's `homogeneous_axes` list (cold path). The agent's job here is to populate the cold-path hints so the very first submission isn't a coin flip.

## Steps

1. **Resolve experiment dir**: `experiment_dir = cwd`. Verify it contains a `.hpc/tasks.py` (or whatever convention the repo uses to express its parallel work).

2. **Inspect the experiment for parallel axes.** Read `tasks.py` and any companion files (`CLAUDE.md`, README, executor scripts) to identify each parallel dimension the experimenter has expressed. The agent should figure this out — the experimenter is not required to declare axes anywhere. Common shapes:
   - A `resolve(task_id)` function that returns kwargs derived from `task_id` via cartesian product over named lists.
   - A grid-search dict the executor reads.
   - An explicit per-axis loop in driver code.
   For each axis, note: name, approximate cardinality, what the values represent.

3. **Classify each axis as homogeneous or not.** "Homogeneous" means tasks differing only on this axis have similar runtime + memory cost. Use the experiment's semantics, not name matching alone. Heuristics that often hold:
   - Replicates / seeds / folds / cross-validation windows / time-series backtest windows → typically **homogeneous** (same compute on slightly different data).
   - Model class / architecture / algorithm → typically **heterogeneous** (orders-of-magnitude different cost between e.g. linear-regression and a deep net).
   - Data type / dataset → depends on dataset sizes; usually mildly heterogeneous.
   - Hyperparameter sweeps → depends; learning rates rarely change cost; layer counts usually do.

4. **Show the user the proposed classification with one-sentence reasoning per axis** and ask for confirmation:

   > Found these parallel axes in your experiment:
   >  • `window` (20 values) — homogeneous (same model trained on a 6-month rolling window)
   >  • `model` (4 values) — heterogeneous (linear / ridge / xgboost / neural_net have very different runtimes)
   >  • `data_type` (3 values) — heterogeneous (equities are 10x larger than fx)
   >
   > I'll write `.hpc/axes.yaml` with `homogeneous_axes: [window]` so the framework promotes `window` to the task array.
   >
   > Looks right? [Y/n]

5. **On Y**, invoke [axes-init](../../docs/primitives/axes-init.md):
   ```
   hpc-mapreduce axes-init --homogeneous-axes <comma-separated-names>
   ```
   If `axes.yaml` already exists, the primitive returns `wrote: false` with a reason. **Re-prompt the user** asking whether to pass `--force` (they may have hand-edited the file). Don't auto-force.

6. **Parse the envelope** — confirm `wrote: true` and the resolved `axes_path`. **On `wrote: false`**, surface the existing file's contents and ask the user how to proceed.

7. **On N at Step 4**, abort without writing. Note that the user can re-run `/hpc-axes-init` later, or write `.hpc/axes.yaml` by hand.

## Notes

- **One-shot per repo** under normal use. If the experiment's parallelism shape changes (axis added, semantics flipped), re-run with `--force`. The framework picks up the new file on the next submit.
- **The picker doesn't require this file to function** — when no `axes.yaml` exists and no priors exist, the picker returns `(None, "no axes.yaml")` and the caller falls back to asking the user explicitly. Running `/hpc-axes-init` makes the cold-start path *automatic* instead of interactive.
- **Cardinality is not yet recorded** in the v1 schema — only `homogeneous_axes` (a list of names). Cardinalities will land when submit-flow integration uses them to build the wave_map; the agent will populate them then.
- **Field-mirror discipline**: the schema permits exactly the fields the framework can act on. Putting search-space definitions or objective functions here is rejected at validation time. Keep that intent in `tasks.py` / executor code where it belongs.
