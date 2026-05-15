# Memory across campaigns

> The `interview` ↔ `recall` feedback loop: how each campaign's intent persists into structured artifacts that ground the next campaign's interview.

## The problem

The conversation between Claude Code (or an external orchestrator) and the human that produces a campaign — *what's being optimized, what range, what's the budget, what's the abort criterion* — used to be transient session context. Once `tasks.py` was submitted, the *why* behind every decision was gone.

Net effect: every interview re-derived "the useful LR range" or "your typical task count" from scratch. The system had no memory across campaigns.

## The two-primitive loop

Every campaign now ends with a structured intent file persisted next to the tasks.py it produced. The next campaign's interview can query a directory of past intents and start grounded in what already happened.

```
                      ┌─────────────────────────────┐
                      │  Claude Code or external    │
                      │  orchestrator interviews    │
                      │  the operator               │
                      └──────────────┬──────────────┘
                                     │
                                     │ produces tasks.py
                                     │ + intent.json
                                     ▼
            ┌─────────────────────────────────────────┐
            │  hpc-agent interview                │
            │  - validates tasks.total() == intent.n  │
            │  - dry-resolve preview (first/mid/last) │
            │  - persists intent verbatim +           │
            │    cmd_sha + materialized_at            │
            └────────────────┬────────────────────────┘
                             │ writes
                             ▼
                  <campaign_dir>/interview.json
                             │
                             │   ... time passes; many campaigns ...
                             │
                             ▼
            ┌─────────────────────────────────────────┐
            │  hpc-agent recall                   │
            │  - walks experiment_roots               │
            │  - filters (task_kind / operator /      │
            │    since)                               │
            │  - returns recency-sorted summaries +   │
            │    rollup (Tier 1 / 2 / 3)              │
            └────────────────┬────────────────────────┘
                             │
                             ▼
                  Next interview starts here
                  ("Your last 5 LR sweeps lived in
                  [1e-6, 1e-1]; tighten this time?")
```

## Anatomy of `interview.json`

Persisted by `hpc-agent interview`:

```json
{
  "goal": "find LR for vit-b on imagenet-1k @ 8 GPUs",
  "task_count": 30,
  "task_kind": "ml-hparam-sweep",
  "budget": {"gpu_hours": 200, "wall_clock_max_h": 12},
  "abort_if": {"metric": "val_loss", "above": 5.0, "after_tasks": 5},
  "cluster_target": {"cluster": "slurm-a100", "profile": "gpu1"},
  "produced_by": {"kind": "human", "operator": "james", "at": "2026-04-30T..."},
  "transcript": [{"role": "agent", "text": "..."}, ...],
  "task_generator": {
    "kind": "numeric_logspace",
    "params": {"param": "lr", "low": 1e-5, "high": 1e-1, "n": 30}
  },
  "_materialized": {
    "at": "2026-04-30T15:23:01+00:00",
    "cmd_sha": "deadbeef...",
    "total_tasks": 30
  }
}
```

Required: `goal`, `task_count`, `produced_by`. Everything else is optional.
The interview primitive writes `_materialized` itself; the rest is the intent payload verbatim.

For the full schema see [`src/claude_hpc/schemas/interview.input.json`](../../src/claude_hpc/schemas/interview.input.json).

## Two ways to produce `tasks.py`

The interview primitive has two modes, picked by whether `intent.task_generator` is set:

**Validate mode** (`task_generator` absent). The interview agent writes `tasks.py` itself — by hand or by running its own scripts to produce a list. The primitive validates the produced file: cross-checks `tasks.total() == intent.task_count`, fingerprints with `cmd_sha`, and persists `interview.json` next to it.

**Generator mode** (`task_generator` present). The primitive writes `tasks.py` from the typed recipe. Five shapes:
- `enumerated` — `params.items: [dict, ...]` literal list
- `cartesian_product` — `params.axes: {k: [vs]}` cross-product
- `items_x_seeds` — every item × every seed
- `numeric_logspace` / `numeric_linspace` — `param` swept over `[low, high]` with `n` points

Generator mode is byte-equivalently idempotent on re-run. To diverge from the recipe, drop `task_generator` from the next intent and the primitive flips back to validate mode for the hand-edited file.

## Querying with `recall`

```bash
hpc-agent recall \
    --root ~/experiments \
    --task-kind ml-hparam-sweep \
    --include-runtime \
    --include-generator-stats \
    --limit 10
```

Returns up to 10 most-recent matching campaigns plus a `rollup` block. Three rollup tiers, increasing in compute cost / opt-in level:

**Tier 1 (always-on)** — invariant aggregations from interview.json fields:
- `count`
- histograms over `task_kind` / `operator` / `produced_by_kind` / `task_generator.kind` / `cluster`
- `task_count` quantiles (linear-interp `p50` / `p95` / `min` / `max`)
- `materialized_at` envelope (`earliest` / `latest`)

`task_kind` is whatever opaque string the caller wrote at interview time; the histogram counts what's there. The example values used throughout this doc (`ml-hparam-sweep` etc.) aren't a canonical taxonomy — pick whatever vocabulary makes sense for your project and reuse it across campaigns so the rollup stays useful.

**Tier 2 (`--include-runtime`)** — walks each matched campaign's `.hpc/runtimes/*.json` and aggregates per-task observations:
- `walltime_per_task_sec` quantiles
- `failure_rate` (from `exit_code != 0`)
- `total_task_samples`
- `campaigns_with_no_runtime` (matched but never produced any runtime data)

**Tier 3 (`--include-generator-stats`)** — buckets matched campaigns by `task_generator.kind` and reports observed parameter envelopes:
- `numeric_logspace` / `numeric_linspace`: `param_envelopes: {param: {low: [min, max], high: [min, max], n: [min, max]}}`
- `cartesian_product`: `axis_value_unions: {axis_name: [unique values seen across campaigns]}`
- `items_x_seeds` / `enumerated`: count only

**Observed ranges only — no recommendations.** The recall primitive is a memory layer; reasoning over the ranges (whether to tighten, widen, or pivot) stays in the calling agent.

## Default `--root` via config

Recall walks one or more directories. To avoid passing `--root` every call, drop a config file:

```json
// ~/.claude-hpc/config.json
{
  "experiment_roots": [
    "/home/user/experiments",
    "/scratch/user/campaigns"
  ]
}
```

When `--root` is omitted, recall walks every path in `experiment_roots`. If neither `--root` nor the config is set, the call fails with `spec_invalid` — no implicit cwd default.

The CLI flag wins when set, so ad-hoc queries against other roots remain frictionless.

## When to call recall

The natural points are:

- **Before starting a new interview** — Claude Code calls `recall --task-kind <kind> --limit 5` and feeds the matched summaries into the conversation as grounding context.
- **At interview-time decisions** — when the operator asks "what range should I sweep?", a recall query with `--include-generator-stats` surfaces the envelope of the last several matching campaigns directly.
- **For sizing decisions** — `--include-runtime` gives walltime / failure-rate baselines from past campaigns of the same kind.

## What recall does *not* do

- It does not fold in per-task `metrics.json` content. Every experiment emits different metrics keys; aggregating across campaigns is where "useful vs noise" really bites. Metric inspection stays a per-campaign concern; the calling agent reads individual `metrics.json` files when it needs them.
- It does not recommend parameter ranges. Observed ranges only.
- It does not maintain a separate index DB. The filesystem is the index. Walks are bounded at 10 000 `interview.json` files per scan.

## See also

- [`primitives/interview.md`](../primitives/interview.md) — interview primitive contract
- [`primitives/recall.md`](../primitives/recall.md) — recall primitive contract
- [`reference/cli-spec.md`](../reference/cli-spec.md) — envelope shape and error_codes
- [`workflows/campaign.md`](campaign.md) — closed-loop campaign iteration (a sibling memory pattern, scoped to one campaign rather than across them)
