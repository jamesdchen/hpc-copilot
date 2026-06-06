`/submit-hpc` is the **human-interview wrapper** around the `hpc-submit` skill — the agent-autonomous decision layer that resolves every HPC submission input and hands off to a fresh-context worker. The slash's job is to call the skill, conduct the user-facing dialogs, and surface the result; the skill body owns the handoff CLI (do not shell it from this slash).

Worker startup is slow — `load-context`, the round-trip ssh probes, the rsync deploy, the cluster-side env-activation probe can run for minutes (a live 2026-06-05 submit spent **2m22s** in worker startup while the main loop sat idle). During it the main thread has no worker output to act on. But it *does* have work that runs **ahead of** any worker output: the user-facing questions whose answers are runtime-behaviour knobs (not spec-build inputs), and local config validation. **Overlap them** — dispatch the worker in the background and canvass the user + validate locally in parallel, instead of serialising human-thinking time behind worker-startup time (#286).

This is the layer **above** the in-worker pipeline parallelism of #277–#280 (which overlaps stages *inside* the worker). It is a slash-side change only: the `hpc-submit` skill and the worker contract are unchanged.

## The flow

1. **Parse `$ARGUMENTS`** into an initial spec (whatever the user pre-stated).
2. **Fork.** In one message: (a) dispatch the `hpc-submit` skill in the **background** on the initial spec, running **autonomously** (apply each ambiguity's `safe_default`; do not pause for the user); (b) in the foreground, canvass the predictable runtime-behaviour questions and run the local config probes (see *Parallel startup*).
3. **Join.** When canvassing + probes finish, await the background dispatch. The fast paths (preflight cached, deploy cache hit, nothing to rsync) usually return before you finish canvassing — the await is immediate. Reconcile the user's answers against what the dispatch used.
4. **Cascade.** If a field is still unresolved (the autonomous path couldn't `safe_default` it — a greenfield `entry_point`, a missing `task_generator`), walk the matching dialog and re-invoke. Bounded by the dependency DAG depth (~3 rounds, typically one).
5. On a successful envelope, surface the worker's result.

## Execution style

- **Batch independent tool calls** into one parallel message — multiple reads, greps, or `hpc-agent describe`/`--help` lookups with no data dependency should not run serially.
- **Be terse.** Lead with the action or result; skip filler ("Let me…", "I'll go ahead and…") and trailing restatements of what tool output already shows.

## Parallel startup

### Background dispatch (the worker side)

Invoke the `hpc-submit` skill in the **background** — Claude Code's `Agent` tool supports `run_in_background: true`; dispatch the skill invocation as a background task and keep its task id. Instruct it to run **autonomously**: apply each ambiguity's `safe_default` rather than returning `needs_resolution`, exactly as a non-interactive caller (a MARs experiment-runner) would. It runs the slow startup — preflight, rsync deploy, canary — while you canvass.

This is a *speculative* dispatch: it builds the spec and starts the deploy from the safe defaults, betting that the user's answers won't contradict what it builds. **Most canvassed questions are runtime-behaviour knobs, not spec-build inputs** (whether to overwrite a prior run, how to handle a task-generator mismatch, how many tasks to keep in flight) — so in the common case the background work is exactly reusable and the join is a no-op merge.

### Foreground canvassing + local validation (the main-thread side)

While the background dispatch runs, do the work that needs no worker output:

- **Canvass the predictable runtime questions** — `overwrite_prior_run`, `on_task_generator_mismatch`, the `data_axis` confirmation when the classifier returns `unclassifiable`, `k_in_flight` (the dialogs under *Runtime-behaviour canvass*). These are the questions the worker would otherwise surface one-by-one *after* it fails or hits a `needs_decision`; pulling them forward overlaps the user's thinking time with the deploy.
- **Validate local config** (no worker dependency — use `Read`/`Grep`, never shell): `clusters.yaml` coherence (the modules block; `conda_source` present when `conda_env` is set — the #281 shape, caught laptop-side before the deploy rather than at the cluster preamble); `.hpc/axes.yaml` freshness against the current `@register_run` signature; a working-tree dirtiness check.
- **Surface recent history** for context: the last few journal entries for this `cmd_sha` family, or the prior `metrics.json` summary on a continuation run.

### Join — reconcile, and cancel only on a real conflict

Await the background task, then reconcile:

- **No conflict (the common case)** — the user's answers are runtime-behaviour knobs the built spec doesn't depend on, or they match the safe defaults the dispatch used. Accept the dispatch's envelope; fold the runtime answers into the surfaced result. Done.
- **Conflict (rare)** — an answer changes the spec the dispatch built: `on_task_generator_mismatch=refresh` (rewrites `tasks.py`), the user overrides the classifier's `data_axis` safe_default with a different kind, or `overwrite_prior_run=no` when the dispatch assumed it could claim the `cmd_sha`. Cancel the background task and re-invoke the skill (foreground) with the corrected, now-fully-resolved spec. The cost is low: a cancelled dispatch has done preflight + maybe started rsync, **not** the main-array `qsub`.

### When to skip the parallel path

The overlap only pays when worker startup is actually slow. **Run the simple synchronous path** — invoke the skill in the foreground, walk dialogs on `needs_resolution` — when there is nothing to overlap: no questions to ask (the user pre-stated everything) **and** a warm fast path (preflight cached, deploy cache hit). Backgrounding a sub-second dispatch just adds a join.

## Invocation

Invoke the `hpc-submit` skill via the Skill tool with the initial spec (foreground on the synchronous path; the same call dispatched as a background task on the parallel path):

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

This is the **cascade** path of the join (step 4): fields the autonomous background dispatch could not `safe_default`. The skill returns:

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

## Runtime-behaviour canvass

These are the questions canvassed **in parallel** with the background dispatch (the *Parallel startup* fork). They are mostly runtime-behaviour knobs the built spec does not depend on, so the answer rides the join rather than blocking the deploy. Ask only the ones in play for this invocation.

### Dialog: `overwrite_prior_run`

A prior run for this `cmd_sha` is on disk (the background dispatch reconciles it against the cluster — skill Step 1b). Ask in parallel so the verdict is ready at the join:

```
A run for this exact config already exists: <run_id> (<state>).
  [keep]      (default) — don't resubmit; monitor or aggregate the existing run.
  [overwrite] — claim the cmd_sha and submit fresh.
```

`keep` routes to `/monitor-hpc` (still in-flight) or `/aggregate-hpc` (complete). `overwrite` only proceeds when reconcile shows a *resubmittable terminal* state (`failed`/`abandoned`, never `complete` or live `in_flight` — the #276 predicate). `overwrite=no` after the dispatch assumed it could claim the slot is a *spec conflict* — cancel + route, don't resubmit.

### Dialog: `on_task_generator_mismatch`

A cached `interview.json` encodes a *different* `task_generator` than the one passed this invocation (skill Step 3 surfaces `spec_invalid: task_generator_mismatch`). Ask up front so the answer rides the re-dispatch instead of a second round-trip:

```
The cached interview implies <N_cached> tasks; your request implies <N_caller>.
  [fail]          (default) — stop; don't silently submit the wrong count.
  [refresh]       — rewrite the interview + tasks.py from your request, then submit.
  [prefer-caller] — submit your task_generator without rewriting the interview.
```

`refresh` rewrites `tasks.py` → it changes the built spec → it's a *spec conflict* with a dispatch that assumed the cached generator: cancel + re-dispatch with `on_task_generator_mismatch=refresh`. `prefer-caller` is reusable as-is.

### Dialog: `data_axis` confirmation (`unclassifiable`)

When the autonomous classifier returns `unclassifiable`, the background dispatch falls back to the `Sequential` safe_default (slow, not wrong). Confirm in parallel via the **`data_axis` dialog above** — if the user picks a non-`Sequential` kind, that overrides the safe_default and is a *spec conflict* (cancel + re-dispatch); if they accept Sequential (or are unsure), the dispatch's work stands.

### Dialog: `k_in_flight` (concurrency cap)

How many array tasks to let run at once — the scheduler throttle (`clusters.<cluster>.max_concurrent_jobs`, the cap the throughput planner groups waves under). Default: the cluster's configured value. Ask only when the user signals a concurrency preference or the cluster sets no cap. This never changes the built task set, so it never conflicts with the background dispatch — fold it in at the join.

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
