# Design: the strategy-agnostic campaign seam

> **Status:** implemented. Tracks
> [#218](https://github.com/jamesdchen/hpc-agent/issues/218) /
> [#219](https://github.com/jamesdchen/hpc-agent/issues/219).
> Shipped: `prior_records()`, the `trial_token` reserved-key strip, the
> sidecar `trial_tokens` round-trip, the campaign-iteration dedup rejection,
> the `optuna_strategy.py` / `pbt_strategy.py` scaffolds, **and the
> end-to-end CLI wiring of `trial_token`** — `compute-run-id` extracts the
> per-task tokens (the one place the task list is materialized) and
> `write-run-sidecar` persists them, so `prior_records()["trial_tokens"]` is
> populated through the canonical Step-6d submit path, not just via a direct
> library call.
>
> **Still deferred** (only matters for a concurrent / out-of-order strategy,
> and only on the path that skips Step-6d): threading `trial_tokens` through
> `build-submit-spec` → `SubmitFlowSpec` → `submit_flow._ensure_run_sidecar`,
> the *synthesized*-sidecar fallback. The campaign loop goes through Step-6d
> `write-run-sidecar` first (now wired), so `_ensure_run_sidecar` is a no-op
> for it — this fallback is an edge case, not the core feature, and is left
> for when a pure-agent-submit campaign actually needs it. Also deferred:
> campaign-awareness on the advisory `find-prior-run` primitive (the
> authoritative dedup rejection is already wired in `submit_and_record`).
> For runtime behaviour see
> [`docs/workflows/campaign.md`](../workflows/campaign.md).

## Problem

The campaign loop is the substrate for *closed-loop* experiments: submit
→ observe → decide → submit again. The obvious integration is
hyperparameter optimisation (Optuna/Ax) via ask-tell. The trap is to
generalise *from* ask-tell — e.g. by adding a privileged typed
`objective: float` channel to the framework. That over-fits to one
campaign class and demotes every other to second-class.

The framework must stay **experiment-agnostic**: exactly the property
that makes `tasks.py`'s `resolve(i)` boundary work — the framework calls
it and moves the bytes; the experiment repo owns all meaning.

## Campaign classes, and why a scalar objective is the wrong primitive

"Ask-tell HPO" silently bundles three independent assumptions. Different
campaign classes violate different ones:

| Class | Decision driver | State carried between iterations | Early-kill? |
|---|---|---|---|
| HPO (Optuna/Ax/grid/random) | scalar/vector objective | scalars + token | no |
| Walk-forward / rolling backtest | **deterministic schedule — no objective** | a counter | no |
| Convergence / Monte-Carlo | a **statistic** over accumulated results | running estimate | no |
| Multi-objective / Pareto (NSGA-II) | objective is a **vector / frontier** | population + token | no |
| Population-Based Training | fitness **+ clone-and-perturb** | **checkpoints (artifacts)** | partial |
| RL self-play / iterative distillation | improves a model from **its own outputs** | **weights + replay buffer / corpus** | no |
| Active learning / data acquisition | model uncertainty → **which points to label** | **growing labeled set** | no |
| Hyperband / ASHA / async-PBT | intra-run intermediate values | scalars | **yes** |
| Multi-stage pipeline | sequential dependency | data between stages | no |

Three things break:

1. **No objective exists** — walk-forward, curriculum (driven by a schedule/count).
2. **The objective isn't a scalar** — Pareto vectors; or a statistic like accumulated variance.
3. **The carried state is an artifact, not a number** — PBT checkpoints, RL
   replay buffers, active-learning label sets, distillation corpora. This is
   the common, high-value case for autonomous-research-agent workloads.

A privileged scalar objective serves only the first row. It would make
the artifact-carrying and schedule-driven classes second-class — the
opposite of agnostic.

## What the framework already gets right

Today's design is *already more general than Optuna* and must not regress:

- `prior(experiment_dir, campaign_id)` returns **opaque per-iteration
  reduced-metric dicts** — arbitrary shape, no ascribed meaning.
- `campaign_dir()` reserves `.hpc/campaigns/<cid>/` for **arbitrary**
  strategy state (Optuna SQLite, PBT checkpoints, walk-forward cursor).
  The framework writes nothing inside.

## The seam: three universal pieces, zero objective concept

### 1. `trial_token` — opaque round-trip

Promote today's `_optuna_trial_number` leading-underscore convention to a
first-class field on the submit spec / `resolve()` return. The framework
guarantees it is (a) carried into the run sidecar verbatim and (b)
re-exposed paired with that iteration's results — and **never
interpreted**. It is bytes.

| Strategy | What it puts in `trial_token` |
|---|---|
| Optuna / Ax | `trial.number` |
| PBT | `(member_index, generation)` |
| Active learning | acquisition-batch id |
| Walk-forward | nothing (windows self-identify) |

### 2. Campaign-iteration dedup salt

`cmd_sha` is the SHA-256 of the materialised task list, which makes
re-submits dedup automatically — correct for a static `tasks.py`, a
footgun for any campaign that *deliberately* re-runs equal params
(Monte-Carlo accumulation, RL same-hyperparams-per-generation, the
documented stochastic-HPO collision in
[`campaign.md`](../workflows/campaign.md)). Fix it in the framework:
salt `cmd_sha` with the campaign-iteration ordinal (`len(prior)`) for
campaign-tagged submits, so iteration N never dedups against iteration M.

This frees `trial_token` to be *purely* a reconciliation token instead of
doubling as a dedup-buster the user has to hand-inject into `resolve()`.
Coordinate with [#207](https://github.com/jamesdchen/hpc-agent/issues/207)
(cmd_sha param-identity semantics).

### 3. Artifact / result-dir lineage in `prior_records()`

A new accessor `prior_records()` (sibling to the unchanged `prior()`)
exposes each past iteration's `result_dir` paths (it already runs
`reduce_metrics` over them — it has them). This single addition unlocks the
artifact-carrying classes:

- **PBT** — locate the checkpoint to clone.
- **RL self-play** — locate the previous generation's replay buffer.
- **Active learning** — locate the prior label set to extend.
- **Distillation** — locate the generated corpus.

Still fully opaque: the framework hands back paths; the strategy decides
what's inside.

### The objective stays a user-owned metrics key

There is no framework `objective`. HPO reads `metrics["val_loss"]`;
convergence reads `metrics["estimate"]` and computes its own variance;
walk-forward reads nothing. The framework never knows which key (if any)
is "the objective", nor the optimisation direction.

## `prior_records()` return shape (as implemented)

```python
# prior_records(experiment_dir, campaign_id) -> list[record]  (oldest-first)
{
    "run_id": "…",
    "campaign_id": "…" | None,
    "trial_tokens": [<opaque, round-tripped from resolve()>] | None,
    "result_dirs": ["…"],   # per-task output dirs — artifact lineage
    "metrics": {…},         # reduce_metrics(result_dirs) — same payload as prior()
    "complete": bool,       # filesystem-derived: any result_dir has a metrics.json
}
```

`prior()` is unchanged (still returns just the `metrics` dict per
iteration). `complete` is a pure filesystem readiness flag, **not**
authoritative lifecycle — `failed` vs `timeout` vs `abandoned` live in the
journal and are reported by `hpc-agent status`. Keeping `prior_records` a
sidecar+filesystem read (no SSH, no journal) is what makes it safe to call
from `tasks.py` at module load.

## Worked examples

Both ship as tested, **cluster-safe** scaffolds (the cluster imports
`tasks.py` and calls `resolve()` on the compute node, so the optimizer must
not be imported / re-`ask`ed there). They are also **load-idempotent**:
validators (`validate-campaign`, `dry-run-local`), `compute_cmd_sha`, and
`--dry-run` submit paths all import the module and call `total()`/`resolve()`,
so a strategy must index proposals by completed count — never by counting
on-disk artifacts a prior load itself created, which would mint a phantom
optimizer trial per validation pass. See
[`optuna_strategy.py`](../../src/hpc_agent/execution/mapreduce/templates/scaffolds/optuna_strategy.py)
and
[`pbt_strategy.py`](../../src/hpc_agent/execution/mapreduce/templates/scaffolds/pbt_strategy.py).

### Optuna (scalar objective lives in a metrics key)

`import optuna` + `study.ask()` happen only on the orchestrator (inside a
`_propose` helper), keyed by the count of completed iterations so the index
is identical on orchestrator and cluster; `resolve()` reads the persisted
proposal. Reconciliation is by oldest-first index (record `i` == trial `i`):

```python
for i, rec in enumerate(prior_records(".", CID)):
    if rec["complete"] and study.trials[i].state == RUNNING:
        study.tell(study.trials[i], rec["metrics"]["val_loss"])  # "val_loss" is just a key
```

`val_loss` is a user-chosen metrics key — the framework privileges no
objective. The rough edges in the old Recipe 2 (the `pass`/`__import__`
junk, the executor-side `tell`) are gone.

### Synchronous PBT (artifact lineage, no scalar-objective channel)

```python
gens = [r for r in prior_records(".", CID) if r["complete"]]   # finished generations
survivors = sorted(members(gens[-1]["result_dirs"]),          # read checkpoints from result_dirs
                   key=lambda m: m["fitness"], reverse=True)[: POP // 2] if gens else []
def total():   return 0 if len(gens) >= MAX_GEN else POP
def resolve(member):
    if not survivors:
        return {"lr": fresh_lr(member), "init_ckpt": "", "trial_token": [len(gens), member]}
    parent = survivors[member % len(survivors)]
    return {"lr": perturb(parent["lr"], len(gens), member),
            "init_ckpt": parent["ckpt"], "trial_token": [len(gens), member]}
```

`fitness` is just a key; `result_dirs` carries the checkpoints; the
perturbation is seeded by `(generation, member)` so `resolve` is
deterministic on both the orchestrator and the cluster. The framework
imports neither an optimiser nor a notion of "fitness".

## Out of scope (deliberate exclusions)

- **Early-kill (Hyperband / ASHA / async-PBT)** — requires terminating
  running trials, colliding with the no-`scancel` invariant
  ([CONTRACT.md §Cancel/abort](../integrations/CONTRACT.md)). Synchronous
  "finish then select / don't-propagate" variants fit; async early-kill is
  a separate decision ([#228](https://github.com/jamesdchen/hpc-agent/issues/228)).
- **True DAG pipelines** — inter-stage dependency is Snakemake/Nextflow's
  job. A campaign is *iteration*, not a pipeline.

## Related issues

- [#218](https://github.com/jamesdchen/hpc-agent/issues/218) — this design (tracking)
- [#219](https://github.com/jamesdchen/hpc-agent/issues/219) — tested Optuna + PBT scaffolds
- [#207](https://github.com/jamesdchen/hpc-agent/issues/207) — cmd_sha semantics (dedup-salt coordination)
- [#228](https://github.com/jamesdchen/hpc-agent/issues/228) — early-kill vs no-scancel
