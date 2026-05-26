`/submit-hpc` is the **human-interview wrapper** around the `hpc-submit` skill — the agent-autonomous decision layer that resolves every HPC submission input and hands off to the submit-flow worker.

When the user types `/submit-hpc`, this slash walks the propose-then-confirm dialog for each decision point, then invokes the `hpc-submit` skill (via the Skill tool) in `mode: "interview"` with the user-resolved fields. The skill skips its own autonomous resolution for any field the user already pinned, fills in the rest from on-disk state, and shells out to the submit-flow worker.

The decision content (which tree branch resolves the data axis, which entry-point pathway, etc.) lives in the **skill** — the canonical source of truth. This slash's job is purely human-elicitation: walking the user through each decision the skill would otherwise auto-resolve.

## Interview

For each of the following decisions, conduct the dialog *only if the user didn't already state the value* in `$ARGUMENTS`. Skip decisions the user pre-stated.

### Cluster

If `clusters.yaml` has exactly one configured cluster, default to it. Otherwise:

```
Configured clusters: hoffman2, discovery, frontera.
Which cluster?
```

### Entry-point onboarding (when no `@register_run` on disk)

Probe the repo:

```bash
ls main.py train.py run.py experiment.py 2>/dev/null
ls src/main.py src/train.py src/run.py 2>/dev/null
find . -maxdepth 4 -name __main__.py -not -path '*/.*' 2>/dev/null | head -5
test -f pyproject.toml && grep -A1 '\[project.scripts\]' pyproject.toml 2>/dev/null
ls run.sh launch.sh ./simulator 2>/dev/null
```

If nothing matches (greenfield):

```
I don't see an entry-point file. I can scaffold one. Two shapes:
  [1] script   (default) — train.py with @register_run + argparse.
  [2] notebook — notebooks/experiment.ipynb with @register_run.
Which shape?  [1 / 2]
```

If multiple candidates exist:

```
I see plausible entry points: train.py, eval.py, python -m mypkg.cli.
Which one should the cluster run?
```

Direct decoration is the default; only fall back to a wrapper when blocked (non-Python entry point, `@hydra.main`, consuming click/typer decorator, vendor code):

```
Your train.py parses --config and --seed via argparse. The cleanest onboarding
is @register_run direct decoration — a two-line edit (import + decorator).
Apply this edit?  [Y / n / show me first / use a wrapper instead]
```

### Data axis classification (when no `DataAxis` in `axes.yaml`)

Read the run's source. Walk the decision tree (from `hpc_agent/experiment_kit/axis.py`):

1. Each row independent of prior rows? → **Independent** (DOALL).
2. Carried state combinable in any order (sum / moments)? → **Associative** with monoid.
3. Bounded look-back (rolling window)? → **BoundedHalo** with `halo.expr`. Bias the halo **large**.
4. Otherwise / unsure → **Sequential** (fail-safe; serial is slow, not wrong).

Propose with one-sentence reasoning:

```
Your run `forecast` iterates an 8760-row hourly series. The loop refits the
model on a trailing `train_window`-day window each step — a bounded look-back.
I'll classify it as: DataAxis = BoundedHalo, halo = train_window * 48
Looks right?  [Y / n / unsure]
```

On **unsure**, fall back to Sequential.

### Homogeneous axes (when no `.hpc/axes.yaml`)

Read `tasks.py`. Classify each named dimension by heuristic (seeds/folds/windows are homogeneous; model class, dataset, layer count are heterogeneous). Propose:

```
Found parallel axes:
 • `window` (20 values) — homogeneous
 • `model` (4 values) — heterogeneous
I'll write .hpc/axes.yaml with homogeneous_axes: [window].
Looks right? [Y/n]
```

### Frozen YAML configs

For each `configs/*.yaml`:

```
Treat configs/exp_42.yaml as a frozen experiment config?  [Y / n / different file]
```

### Task generator

If neither caller-supplied nor inferable from existing `tasks.py`, ask:

```
What's the scale-up shape?
  items_x_seeds      — one frozen config × N seeds
  cartesian_product  — cross several axes (seed × shard × ...)
  enumerated         — hand-supplied list of N task dicts
  numeric_linspace   — sweep one numeric hyperparameter (linear)
  numeric_logspace   — sweep one numeric hyperparameter (log)
```

Walk the user through the params for their choice.

## Handoff

Assemble every user-resolved value into the fields dict, then invoke the `hpc-submit` skill via the Skill tool:

```
Skill("hpc-submit", {
  experiment_dir: ".",
  cluster: "<resolved>",
  entry_point: <resolved>,        // omit to let skill auto-resolve
  data_axis: <resolved>,           // omit to let skill auto-resolve
  homogeneous_axes: <resolved>,    // omit to let skill auto-resolve
  task_generator: <resolved>,      // omit to use existing tasks.py
  no_canary: false,
  campaign_id: <if set>,
  mode: "interview"
})
```

The skill resolves anything not in the dict, hands off to the submit-flow worker, and returns the envelope. Surface the worker's `data.report.result` (run_id, job_ids, scheduler state), `data.report.decisions`, and `data.report.anomalies`.

## On `spec_invalid` from the skill

The skill returns `spec_invalid` when a decision genuinely needs human intervention (typically: an ambiguity that the user can resolve but the skill won't guess at, like multiple `@register_run` functions). Walk the user through the matching dialog above for the field named in the envelope's `error_code`, then re-invoke with the augmented spec.

Common error codes:
- `ambiguous_cluster` — user picks from the listed candidates
- `ambiguous_entry_point` — user picks from the listed paths
- `ambiguous_run` — user picks which `@register_run` function
- `task_generator_required` — user supplies the shape + params
- `incomplete_aggregation` (from a prior submit's aggregation) — user decides whether to retry or proceed with partial
- `high_failure_rate` (from a prior submit's status) — user investigates before resubmitting

## Args

`$ARGUMENTS` formats:

- **Free-form intent**: `"run ridge with horizon=[1, 5, 25]"` — slash parses to `(executor_hint, axis_hint)` and passes as the skill's `task_generator` params.
- **Flags**:
  - `--cluster <name>` — pin the target cluster (skip the cluster prompt).
  - `--no-canary` — skip the 1-task canary submission.
  - `--campaign-id <slug>` — tag this submission as one iteration of a closed-loop campaign. Required when invoked from `/campaign-hpc`.
- **Empty**: full interactive interview.

## Common failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `Eqw` state (SGE) | Job error | `qmod -cj <JOBID>` or resubmit |
| `PENDING` (SLURM) > 30 min | Resource unavailable | Check `sinfo`, try different partition |
| Memory exceeded | Exceeded mem limit | Resubmit with higher memory |
| Walltime exceeded | Exceeded time limit | Resubmit with longer walltime |
| `ModuleNotFoundError` | Env not set up | Check modules and `conda_env` |
| rsync/scp failure | SSH key issue | `ssh $SSH_TARGET hostname` first; verify `ssh-add -l` |
| `--features` not recognized | Executor doesn't support that arg | Check `--help`, update executor |

When the user mentions CLI arguments the executor doesn't accept (e.g. "sweep features=[har, pca]" but `--features` isn't in `--help`), surface it: "ml_ridge.py doesn't accept `--features`. Should I add it, or did you mean a different executor?"
