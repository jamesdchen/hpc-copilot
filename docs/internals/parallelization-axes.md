# Parallelization axes — the five-axis model

hpc-agent operates on **five distinct parallelization axes**, each
solving a different problem at a different layer of submission. They
compose into the final scheduler-level submission shape. New
contributors often conflate them; this doc lays out what each is for,
how it operates, and how they interact.

## The five axes at a glance

| Axis | Where declared | Who controls | Importance |
|---|---|---|---|
| **Sweep dimensions** (task_generator) | `.hpc/tasks.py` | User explicit | Primary — produces 100-10000× parallelism |
| **Scheduling axis** (homogeneous_axes) | `.hpc/axes.yaml` `homogeneous_axes` | User explicit (via `/hpc-axes-init` interview) | Determines SLURM array index; affects scheduling efficiency, not raw count |
| **Wave structure** (wave_map) | `infra/throughput.py` planner | Framework auto + user constraint hints | Splits sweep into waves with inter-wave deps; budget / monitoring / BSP |
| **Stage DAG** (stages.py) | Multiple `@register_run` functions with explicit I/O paths | User explicit | Pipeline parallelism (train → predict → evaluate) |
| **DataAxis** (axis.py) | `.hpc/axes.yaml` `executors.<run_name>.data_axis` | User via slash, or matcher autonomously | Splits single task into sub-tasks — niche, secondary |

**The actual workhorses are the top two.** The sweep dimensions create
the task count; the scheduling axis decides per-array reservation
shape. The other three are refinements that activate only in specific
cases.

DataAxis gets disproportionate attention because it's theoretically
interesting (the only axis where the framework attempts non-trivial
program analysis), but for the vast majority of workloads the sweep
dimensions provide all the parallelism needed.

## Axis 1: Sweep dimensions (task_generator)

### Purpose

The entire reason to use HPC is fanout. The user has 100 seeds × 4
models × 2 datasets = 800 experimental configurations and wants all 800
evaluated. The `task_generator` is how the user *declares* this fanout
in a form the framework can iterate over.

### Where it lives

`<experiment>/.hpc/tasks.py` — defines two functions:

```python
def total() -> int:
    return 100 * 4 * 2  # = 800

def resolve(task_id: int) -> dict:
    seed = task_id % 100
    model_idx = (task_id // 100) % 4
    dataset_idx = task_id // 400
    return {"seed": seed, "model": MODELS[model_idx], "dataset": DATASETS[dataset_idx]}
```

The framework calls `total()` to know how many tasks to submit, and
calls `resolve(i)` per task to get its kwargs.

### Built-in task_generator shapes

(In `incorporation/build_tasks_py.py`.)

| Shape | When to use |
|---|---|
| `items_x_seeds` | One frozen config × N seeds — the simplest fanout |
| `cartesian_product` | Cross several axes |
| `enumerated` | Hand-supplied list — heterogeneous sweeps |
| `numeric_linspace` / `numeric_logspace` | Single hyperparameter sweep with linear/log spacing |

### Interactions with the rest of the framework

- **`cmd_sha` computation**: `cmd_sha = sha(executor_module + resolve(0).kwargs + ... + resolve(N-1).kwargs + frozen_yaml_shas)`. This is the framework's idempotency key — if two submissions produce the same `cmd_sha`, the second one is a dedup. Lives in `state/runs/compute_cmd_sha.py`.
- **Array submission**: `total()` determines `--array=0-N` for SLURM (or `-t 1-N` for SGE).
- **Per-task kwargs delivery**: the on-cluster dispatcher reads `tasks.py`, calls `resolve(SLURM_ARRAY_TASK_ID)`, runs the executor with those kwargs.
- **Runtime prior keying**: the runtime prior reader keys observed walltimes by `(profile, cluster, cmd_sha)`. Two different sweep shapes have different `cmd_sha`s and different runtime priors.

The user writes the task_generator. The framework handles everything
below.

## Axis 2: Scheduling axis (homogeneous_axes)

### Purpose

A SLURM task array reserves the **same walltime and memory** for every
element of the array. If the sweep has axes that differ in compute cost
(e.g., `model=ridge` runs in 5 min; `model=xgboost` runs in 50 min),
putting them on the same array means reserving 50 min for every
task — `ridge` wastes 45 min of cluster time per task.

The fix: don't put heterogeneous axes on the array. Submit one array
per heterogeneous-axis value, with its own walltime reservation.

That's what `homogeneous_axes` declares: "these sweep dimensions are
similar enough in cost that they can share a reservation; everything
else gets its own sub-submission."

### Where it lives

`<experiment>/.hpc/axes.yaml`:

```yaml
homogeneous_axes:
  - seed
  - dataset
axes:
  seed: 100
  model: 4
  dataset: 2
```

The user declares which axes are homogeneous. The framework reads
`tasks.py` to derive cardinalities, then computes the right submission
layout.

### How it operates

For the 800-task sweep above with `homogeneous_axes: [seed, dataset]`:

- 4 separate array submissions, one per model (the heterogeneous axis)
- Each array is size 200 = 100 seeds × 2 datasets (the homogeneous axes)
- Each array can have its own `--time=<walltime_for_this_model>` and `--mem=<memory_for_this_model>` based on runtime priors

If `homogeneous_axes` were empty (everything heterogeneous), you'd get
800 separate single-task submissions — defeats the purpose. If
`homogeneous_axes` were everything (8 axes flattened to 800), you'd
reserve the max walltime/memory for every task — wasteful. The right
pick is the trade-off the user makes per experiment.

### Interactions

- **`submit-flow-batch`**: the batched submit primitive that runs N sub-submissions with shared rsync. Driven by the homogeneous_axes layout.
- **Runtime priors**: each `(profile, cluster, model)` has its own runtime prior; `homogeneous_axes` decides which dimension keys the prior.
- **`/hpc-axes-init`** (sub-slash via workflow skill): the interview that asks the user "which axes are homogeneous?" — the user's judgement call is what populates `axes.yaml`.
- **Cold-start fallback**: if `axes.yaml` doesn't exist, the framework refuses to guess (returns `spec_invalid: ambiguous_axis_layout`); the user has to declare it.
- **`pick_array_axis`** (in `infra/throughput.py`): helper that picks which homogeneous axis to promote to the SLURM array index when multiple homogeneous axes exist.

### The seam with other axes

`axes.yaml` is the user's per-experiment policy file for *scheduling*.
Two unrelated blocks live in it: `homogeneous_axes` (this axis) and
`executors.<run>.data_axis` (the DataAxis from Axis 5). They're
orthogonal but co-located. They are commonly conflated; the
`hpc-classify-axis` skill explicitly disclaims at its top that the two
are different.

## Axis 3: Wave structure (wave_map)

### Purpose

Two problems:

1. **Scheduler limits**. SLURM/SGE clusters have `MaxSubmitJobs` and `MaxArraySize` limits. Your 800-task array might exceed `MaxArraySize=500`. You can't submit it as one array.

2. **Concurrency control**. A user's queue allowance might be `MaxConcurrentJobs=200`. You can submit 800 tasks but only 200 run at once; the rest wait in `PENDING`. The user might also want to checkpoint between waves — see results from the first 200 before letting the next 200 start (budget control, early stopping, etc.).

Both problems need *wave structure*: chunk the array into sub-arrays,
gate later waves on earlier waves' completion via scheduler
dependencies.

### Where it lives

`infra/throughput.py` — the planner that takes (total_tasks,
max_array_size, max_concurrent_jobs, per_task_walltime) and emits a
wave layout. Not user-facing; framework-internal.

The output is a `wave_map` — a dict like `{wave_1: [0..499], wave_2:
[500..799]}` plus dependency information.

### How it operates

For 800 tasks with `max_array_size=500`, `max_concurrent_jobs=200`:

- Wave 1: `array=0-199` — first 200 tasks
- Wave 2: `array=200-399 --dependency=afterany:<wave1_job>` — next 200, gated on wave 1
- Wave 3: `array=400-599 --dependency=afterany:<wave2_job>` — next 200
- Wave 4: `array=600-799 --dependency=afterany:<wave3_job>` — last 200

Each wave's per-task metrics get aggregated by the per-wave combiner.
Inter-wave aggregation rolls up to the final result.

### Interactions

- **`combine-wave`**: the per-wave aggregator primitive. Runs as a job dependent on the wave's array completion. Reads per-task sidecars; produces a wave aggregate.
- **Monitoring**: `monitor-flow` polls per-wave status; reports "wave 2 of 4 complete" to the user.
- **Mid-flight kill**: the user can stop a campaign at any wave boundary by not letting the next wave's dependency clear.
- **Campaign integration**: each campaign tick is a wave-of-waves — the iteration's outputs gate the next iteration's submission.
- **Backfill optimisation**: small waves fit into backfill windows better than one big array. An optional plugin's `plan-submit` may recommend smaller waves to exploit predicted backfill gaps.

### Why it's framework-internal

The user doesn't see `wave_map` directly — they don't need to. The
framework computes it from cluster constraints + sweep size at submit
time. The user might tune it indirectly (by setting
`target_backfill_window_sec` for the optional planner plugin, or
`max_concurrent_jobs` in their `clusters.yaml`) but they don't write
the wave layout.

## Axis 4: Stage DAG

### Purpose

Some experiments have natural pipelines:
- Stage 1: pre-process data (1 task that loads + transforms)
- Stage 2: train K models on the processed data (K tasks)
- Stage 3: evaluate each model (K tasks)
- Stage 4: aggregate evaluations (1 task)

Without stage support, the user manually submits each phase, waits,
submits the next. Stage DAG support means: declare the pipeline; the
framework handles inter-stage dependencies automatically.

### Where it lives

`state/stages.py` — the multi-stage DAG model. Each stage has:
- A name
- An executor (its own `@register_run` function)
- Inputs (paths it reads, which previous stages produced)
- Outputs (paths it writes, which subsequent stages consume)

### How it operates

The framework constructs a DAG at submit time. Each stage is a task
array. Inter-stage dependencies use the scheduler's mechanism:

- SLURM: `--dependency=afterok:<previous_stage_jobid>`
- SGE: `-hold_jid <previous_stage_jobid>`

The next stage's array stays in `PENDING` until the previous stage's
array completes successfully. The scheduler enforces the gating; the
framework just submits with the right flags.

### Interactions

- **Per-stage everything**: each stage has its own task_generator, its own homogeneous_axes, its own runtime prior, its own wave structure. The framework's machinery applies recursively per stage.
- **Inter-stage data flow**: stage outputs go to shared cluster scratch (`<scratch>/<run_id>/<stage>/`); next stage reads from there. No journaling between stages — it's pure filesystem.
- **Failure handling**: if stage 2 fails, stages 3+ don't run (the dependency is `afterok`, not `afterany`). The recovery flow handles per-stage resubmission.
- **Aggregation**: each stage may have its own aggregator; the final stage typically aggregates everything into final metrics.

### What the user sees

For single-stage experiments (the common case): nothing. They have one
`@register_run` function; the framework treats it as a one-stage DAG
implicitly.

For multi-stage experiments: they write multiple `@register_run`
functions and declare the stage relationships (typically via
input/output path conventions or an explicit stages declaration).

In practice, stage DAG is used for genuinely multi-stage workloads —
most users have a single-stage submit-monitor-aggregate cycle.

## Axis 5: DataAxis (inner-loop classification)

### Purpose

A single task's `run()` body might contain an iteration that *could*
be split further. For example:

```python
@register_run
def run(config: str, seed: int) -> None:
    data = load(config)
    for t in range(0, 10000):  # ← this loop could potentially be parallelized
        result = step(data, t)
        results.append(result)
```

The DataAxis classification asks: is this inner `for` loop:
- Independent (each iteration pure function of input) → split anywhere
- Associative (carried state combinable in any order) → split + combine partials
- BoundedHalo (carried state with bounded look-back) → split with halo overlap
- Sequential (unbounded carried state) → can't split

If you can split, the framework chunks this single task into multiple
sub-tasks (one per chunk) and combines results.

### Why it's NOT the privileged axis

This is worth being explicit about because the framework's
documentation has sometimes treated DataAxis as central. It's not.

The framework already does map-reduce at the task-array level via
`combine-wave` (see Axis 1 + the map-reduce machinery in
`execution/mapreduce/`). Every submission is shaped as map-reduce — N
tasks fan out (map); per-wave combiner aggregates per-task metrics
(reduce). This is the framework's default mode of operation.

DataAxis is asking "can each of those N tasks be ALSO split
internally?" — a refinement, not the main event. For 99% of workloads,
each task is short enough that the answer is "no, just run it serially
within the task." The framework's sweep parallelism is sufficient.

The cases where DataAxis matters: stencil PDE solvers, rolling-window
backtests with explicit halo, online learning with bounded-memory
updates. These are exotic enough to warrant either human classification
via slash interview or pattern-matched autonomous classification via
the matcher.

### Where it lives

- `experiment_kit/axis.py` — type definitions (the four `DataAxis` classes)
- `experiment_kit/axis_matcher.py` — pattern-matcher for autonomous classification
- `<experiment>/.hpc/axes.yaml` `executors.<run_name>.data_axis` — recorded classification per run
- `experiment_kit/elision.py` — `assert_elision_equivalent` runtime check that whole-vs-split matches

### The matcher's autonomous scope

The matcher classifies only these cases without LLM help:

- **Independent** — no carried outer-scope state in the loop body
- **BoundedHalo** via pattern library: first-order stencil, finite-order stencil, bounded-window deque, pandas rolling, EMA / exponential smoothing
- **Sequential** — carried state but no recognized halo pattern (safe default)

Everything else returns `unclassifiable` and falls through to the LLM
decision tree in the `hpc-classify-axis` skill (rarely invoked).

**Associative is NOT autonomously classified.** Users who want to
parallelize an inner reduction express it as a sweep dimension in their
`task_generator` instead (the framework's existing map-reduce machinery
handles it via Axis 1).

### Interactions

- **`task_generator` interaction**: produces N tasks. DataAxis on each task could further-split into N×K sub-tasks at submit time. But the framework currently doesn't do this auto-splitting for the user's sweep — DataAxis is more useful when the user has ONE task with a long inner loop.
- **Elision gate**: `assert_elision_equivalent` runs the whole-vs-split equivalence check on a small fixture before any cluster time. Catches misclassifications.
- **Halo expression**: `axis_config.py` validates halo expressions are safe arithmetic over `run()` parameters. Used by BoundedHalo classifications.
- **Recall**: prior classifications get stored in campaign summaries; the matcher can reuse them on similar experiments.

## How the five axes compose

A concrete example. The user has an experiment:

```python
# notebooks/forecast.ipynb
@register_run
def run(model: str, seed: int, window: int) -> None:
    data = load_data()
    for t in range(window, len(data)):
        train = data[t-window:t]
        m = fit(model, train, seed=seed)
        pred = m.predict(data[t])
        store_result(pred)
```

The user declares:
- `tasks.py`: `cartesian_product(model=["a","b","c","d"], seed=range(100), window=[10, 30])` → 800 tasks
- `axes.yaml`: `homogeneous_axes: [seed, window]`; `executors.run.data_axis: independent` (each iteration just trains on a window — Independent at the inner level)

`clusters.yaml` declares `max_array_size=500`,
`max_concurrent_jobs=200` for the target cluster.

What the framework does at submit time:

1. **Sweep dimensions** produce 800 task points.
2. **Scheduling axis** says "model is heterogeneous; seed × window is homogeneous." → 4 separate array submissions (one per model), each of size 100×2=200 tasks. Each model's array gets its own walltime/memory tuned to that model's cost.
3. **Wave structure** says "200 tasks per model, max_concurrent=200, so each model is one wave of 200." Total: 4 array submissions, no inter-wave dependencies (each model's array fits in concurrency).
4. **Stage DAG**: single stage (the experiment has one `@register_run`). The DAG is trivial.
5. **DataAxis**: `independent`. The inner loop is just windowed-input slicing with no carried state. No further chunking needed.

The user sees ONE submission — they invoked `/submit-hpc` once. The
framework's machinery decomposes it into 4 array submissions, one per
model, with the right per-array resources.

## How each axis hooks into the framework

Mapping each axis to the primitives, state, and validators that consume it:

| Axis | Reads from | Writes to | Validated by |
|---|---|---|---|
| Sweep dimensions | `tasks.py.resolve()` | `cmd_sha` per task | `validate-input-dataset`, `validate-executor-signatures`, `validate-campaign` (per-task kwargs check) |
| Scheduling axis | `axes.yaml.homogeneous_axes` | Array submissions per heterogeneous-axis value | `axes-init` writes; no specific validator |
| Wave structure | Cluster constraints + total task count | `wave_map` (transient at submit time) | `plan-throughput` produces; an optional plugin's `plan-submit` may re-tune |
| Stage DAG | Multiple `@register_run` functions + I/O paths | Inter-stage dependencies | `stages.py`'s loader validates DAG consistency |
| DataAxis | `axes.yaml.executors.<run>.data_axis` | Per-task chunking spec | `assert_elision_equivalent` runtime check |

Each axis has:
- A declaration mechanism (user-facing or framework-derived)
- A consumer in the submit pipeline (the primitive or component that uses it)
- A validation hook (to catch misuse before cluster time)

## Why this structure

The axes exist because the *scheduling problem* is multi-dimensional.
SLURM/SGE schedulers think in terms of arrays + dependencies +
reservations; users think in terms of "I want to try N hyperparameters
on K datasets." The five axes are the translation layer.

Could you flatten them into one big "submission spec"? In principle
yes — but each axis has a distinct trigger (user need), a distinct
mechanism (where it's expressed), and a distinct optimization story
(what knob it tunes). Keeping them separate makes each one
comprehensible in isolation; collapsing them would create a god-object
spec that's hard to reason about.

## See also

- [`skill-policy.md`](skill-policy.md) — the three-layer / four-surface model for agent-facing markdown
- [`adding-a-primitive.md`](adding-a-primitive.md) — the wire-surface recipe
- [`submit-sequence.md`](submit-sequence.md) — end-to-end walkthrough of a submission
- [`state-model.md`](state-model.md) — what gets persisted where
