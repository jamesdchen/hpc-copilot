`/submit-hpc` is the **human-interview wrapper** around the `hpc-submit` skill — the agent-autonomous decision layer that resolves every HPC submission input and hands off to a fresh-context worker. The slash's job is to call `Skill("hpc-submit", …)`; the skill body owns the handoff CLI (do not shell it from this slash).

The slash conducts the user-facing dialogs **after** the skill identifies what needs resolving. The flow is:

1. Parse `$ARGUMENTS` into an initial spec (whatever the user pre-stated).
2. Invoke the `hpc-submit` skill via the Skill tool with that initial spec.
3. If the skill returns `needs_resolution` with an `ambiguities` list, walk a dialog with the user for each ambiguity (in topo-sorted dependency order), assemble the user's answers into an augmented spec, re-invoke the skill.
4. Repeat up to ~3 times (bounded by the dependency DAG depth — typically once).
5. On a successful envelope, surface the worker's result to the user.

## Execution style

- **Batch independent tool calls** into one parallel message — multiple reads, greps, or `hpc-agent describe`/`--help` lookups with no data dependency should not run serially.
- **Be terse.** Lead with the action or result; skip filler ("Let me…", "I'll go ahead and…") and trailing restatements of what tool output already shows.

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

<!-- decision-content:axis-tree start -->
1. **Does each row's result depend on rows computed before it?**
   No → **`Independent`**. The loop body is a pure function of its row (a DOALL loop) — split anywhere.
2. **Yes → is the carried state a fixed-size summary combinable in any order** — a sum, a count, a mean/variance via moments?
   Yes → **`Associative`**. Pick the monoid: `sum` (additive) or `moments` (mean/variance via sufficient statistics). Default `moments`.
3. **Is the dependence a bounded look-back** — e.g. a rolling training window of N rows?
   Yes → **`BoundedHalo`**. Derive the halo as an arithmetic expression over `run()`'s parameters (bare names), e.g. `train_window * 48`. Bias the estimate **large** — an over-wide halo is merely wasteful; a too-small halo is silent corruption.
4. **Otherwise, or ambiguous → `Sequential`.** This is the fail-safe default and the autonomous-mode tiebreaker. From `axis.py`: *"When in doubt, classify as Sequential: the fail-safe outcome is slow, not wrong."*
<!-- decision-content:axis-tree end -->

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

### Dialog: `uncovered_param`

The executor requires a param you didn't sweep, and it has no default — every task would crash without a value. For each name in `ambiguity.context.required_no_default`:

```
Executor `<run_name>` takes `--<param>` but you didn't declare it as an axis.
Use a fixed value for every task?  [<safe_default if any, else "(no default — please provide)">]
```

Collect the answers into `{<param>: <value>}` and pass them as the resolved `uncovered_param`. (To sweep it instead, the user restates `/submit-hpc` with `<param>=[...]` as an axis.)

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
| `worker authentication unavailable` / worker "Not logged in" | Session is OAuth/subscription; the `--bare` worker can't use it | `export ANTHROPIC_API_KEY=...` (or cloud creds) before launching Claude Code |
