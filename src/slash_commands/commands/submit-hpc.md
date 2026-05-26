`/submit-hpc` is the **human-interview wrapper** around the `hpc-submit` skill — the agent-autonomous decision layer that resolves every HPC submission input and hands off to the submit-flow worker.

The slash conducts the user-facing dialogs **after** the skill identifies what needs resolving. The flow is:

1. Parse `$ARGUMENTS` into an initial spec (whatever the user pre-stated).
2. Invoke the `hpc-submit` skill via the Skill tool with that initial spec.
3. If the skill returns `needs_resolution` with an `ambiguities` list, walk a dialog with the user for each ambiguity (in topo-sorted dependency order), assemble the user's answers into an augmented spec, re-invoke the skill.
4. Repeat up to ~3 times (bounded by the dependency DAG depth — typically once).
5. On a successful envelope, surface the worker's result to the user.

## Invocation

Invoke the `hpc-submit` skill via the Skill tool with the initial spec:

```
Skill("hpc-submit", {
  experiment_dir: ".",
  cluster: <if user stated --cluster>,
  no_canary: <if user stated --no-canary>,
  campaign_id: <if --campaign-id>,
  task_generator: <if inferable from $ARGUMENTS, else omit>
})
```

The skill resolves the rest. The slash never needs to enumerate every field — only what the user supplied.

## On `needs_resolution` — walking the ambiguities

The skill returns:

```json
{
  "ok": false,
  "error_code": "needs_resolution",
  "data": {
    "resolved": { ... fields the skill auto-resolved ... },
    "ambiguities": [
      {"field": "<name>", "candidates": [...], "depends_on": [...], "safe_default": ..., "context": {...}}
    ]
  }
}
```

Topo-sort `ambiguities` by `depends_on`. Walk in order; for each, conduct the matching dialog below. Collect user answers into the augmented spec. Re-invoke.

If `depends_on` has unresolved entries in the current list (cascading dependencies), walk only the resolvable subset this round; the skill will surface a new round of ambiguities (now smaller) after the deps are resolved. Bounded by DAG depth (~3 rounds max for HPC submission).

### Dialog: `cluster`

```
Configured clusters: <candidates from envelope>.
Which cluster?
```

If the user has no preference, accept the `safe_default` from the envelope.

### Dialog: `entry_point`

The skill's sub-skill (`hpc-wrap-entry-point`) couldn't auto-resolve. The ambiguity's `candidates` lists what was found.

**Greenfield (no candidates)**:

```
I don't see an entry-point file. I can scaffold one. Two shapes:
  [1] script   (default) — train.py with @register_run + argparse.
  [2] notebook — notebooks/experiment.ipynb with @register_run.
Which shape?  [1 / 2]
```

**Multiple candidates**:

```
I see plausible entry points: <list from candidates>.
Which one should the cluster run?
```

Then: direct decoration is the default; only fall back to a wrapper when blocked (non-Python entry point, `@hydra.main`, vendor code):

```
Your <path> parses --<flag> via argparse. The cleanest onboarding is @register_run
direct decoration — a two-line edit. Apply this edit?
  [Y / n / show me first / use a wrapper instead]
```

### Dialog: `data_axis`

Walk the decision tree (from `axis.py`):

1. Each row independent of prior rows? → **Independent** (DOALL).
2. Carried state combinable in any order (sum / moments)? → **Associative** with monoid.
3. Bounded look-back (rolling window)? → **BoundedHalo** with `halo.expr`. Bias the halo **large**.
4. Otherwise / unsure → **Sequential** (fail-safe).

Propose with reasoning:

```
Your run `<name>` iterates a <N>-row series. The loop <pattern>.
I'll classify it as: DataAxis = <kind>, <params>.
Looks right?  [Y / n / unsure]
```

On **unsure**, accept `safe_default` (typically Sequential).

### Dialog: `homogeneous_axes`

The skill's sub-skill found parallel axes but couldn't classify their homogeneity. Propose per heuristic (seeds/folds/windows homogeneous; model class, dataset, layer count heterogeneous):

```
Found parallel axes:
 • `<axis>` (N values) — <homogeneous|heterogeneous> (reasoning)
I'll write .hpc/axes.yaml with homogeneous_axes: [<list>].
Looks right? [Y/n]
```

### Dialog: `frozen_configs`

For each `configs/*.yaml` in `ambiguity.candidates`:

```
Treat configs/<name>.yaml as a frozen experiment config?  [Y / n / different file]
```

### Dialog: `task_generator`

The skill refused — this can't be auto-invented. Ask the user:

```
What's the scale-up shape?
  items_x_seeds      — one frozen config × N seeds
  cartesian_product  — cross several axes
  enumerated         — hand-supplied list of N task dicts
  numeric_linspace   — sweep one numeric hyperparameter
```

Walk the user through the params for their choice.

## On final envelope

Surface to the user:
- `data.report.result` (run_id, job_ids, scheduler state)
- `data.report.decisions` (every resolved choice + source)
- `data.report.anomalies`

## Args

`$ARGUMENTS` formats:
- Free-form intent: `"run ridge with horizon=[1, 5, 25]"` — parse to `task_generator` params.
- Flags: `--cluster <name>`, `--no-canary`, `--campaign-id <slug>`.
- Empty: invoke skill with `{experiment_dir: "."}`; skill returns ambiguities; walk dialogs.

## Common failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `Eqw` state (SGE) | Job error | `qmod -cj <JOBID>` or resubmit |
| `PENDING` (SLURM) > 30 min | Resource unavailable | Try different partition |
| Memory exceeded | Exceeded mem limit | Resubmit with higher memory |
| Walltime exceeded | Exceeded time limit | Resubmit with longer walltime |
| `ModuleNotFoundError` | Env not set up | Check modules and `conda_env` |
| rsync/scp failure | SSH key issue | `ssh $SSH_TARGET hostname` first; verify `ssh-add -l` |
