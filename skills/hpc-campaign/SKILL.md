---
name: hpc-campaign
description: "Inspect and run closed-loop campaigns: tagged sequences of submit-flow → monitor-flow → aggregate-flow whose tasks.py reads prior history to decide what to run next."
allowed-tools: Bash Read Write
---

Closed-loop campaigns let an experiment's `tasks.py` adapt iteration-by-iteration based on prior results. The framework provides two things — a `campaign_id` tag on every submit (carried by [submit-flow](../../docs/primitives/submit-flow.md)) and the [campaign-status](../../docs/primitives/campaign-status.md) accessor (called from inside `tasks.py`). The "loop" is repeated `submit-flow → monitor-flow → aggregate-flow` triplets sharing the same `campaign_id` slug — workflow-atom composition with no agent in the per-iteration critical path. Strategies (Optuna, RandomSearch, walk-forward, PBT) live as Python libraries the user imports in their own `tasks.py`. The framework ships **zero** strategy code.

## When to use

- The user mentions hyperparameter tuning, walk-forward backtesting, active learning, population-based training, or any pattern where iteration N's submission depends on iteration N-1's results.
- The user has run `submit-flow` (directly or via `hpc-submit`) before and wants to follow up adaptively.

For one-shot parallel work with no feedback loop, use `hpc-submit` directly.

## Inspection

1. **List every campaign** in this experiment: invoke [campaign-list](../../docs/primitives/campaign-list.md). Empty list if no tagged sidecars exist yet.

2. **Per-iteration history** for one campaign: invoke [campaign-status](../../docs/primitives/campaign-status.md) with `--campaign-id <id>`. The primitive returns iteration count, in-flight count, oldest-first per-iteration reduced metrics (pending iterations contribute `{}`), and the run_ids tagged with this campaign.

## Tagging a submission as part of a campaign

Pass `campaign_id: "<slug>"` in the [submit-flow](../../docs/primitives/submit-flow.md) spec. The slug must match `^[A-Za-z0-9._\-]+$`. The atom threads it onto the per-run sidecar (v2 schema) and the scheduler templates re-export `HPC_CAMPAIGN_ID` to the cluster; the user's `tasks.py` reads it via `os.environ` and calls [campaign-status](../../docs/primitives/campaign-status.md) to get the campaign's history before deciding what to run next.

## Driving the loop

Per iteration, three workflow-atom invocations:

1. **Submit**: invoke [submit-flow](../../docs/primitives/submit-flow.md) with `campaign_id` set. `tasks.py` is re-imported during scaffolding, so `_PRIOR = prior(".", os.environ["HPC_CAMPAIGN_ID"])` sees every previously-completed iteration before deciding what to submit.
2. **Monitor**: invoke [monitor-flow](../../docs/primitives/monitor-flow.md) with the returned `run_id`. Polls until terminal or budget elapses; returns `lifecycle_state` ∈ `{complete, failed, abandoned, timeout}`.
3. **Aggregate** (optional, when the strategy needs cross-wave reduced metrics): invoke [aggregate-flow](../../docs/primitives/aggregate-flow.md). For per-trial-QLIKE-style strategies, this is where the metric the strategy will `tell()` comes from; for simpler strategies that read per-task reduce JSONs directly, skip this step.
4. **Decide**: re-import `tasks.py` and check `tasks.total()`. If `> 0`, go to Step 1. Else done.

Three CLI calls per iteration, all emitting the same JSON envelope shape. The same loop runs identically under Claude Code, cron, or external orchestrators (MARs) because composition is at the CLI-atom level.

Concurrency is opt-in: invoke `submit-flow` again before the previous iteration's `monitor-flow` returns if you want K iterations in flight (Optuna's `constant_liar=True` is built for this). Default to sequential when in doubt.

For headless overnight runs, wrap the loop in a recurring trigger (e.g. `/loop 30m bash .hpc/campaigns/<slug>/iterate.sh`) — `tasks.total() == 0` halts it automatically.

Resume after a network drop or laptop sleep is trivial: there is no driver state to recover. Re-run `campaign-status` to see what landed, then resume the loop. Sidecars on disk are the only durable state.

## Notes

- **No automatic retry at the campaign level.** A single iteration's failure surfaces in `submit-flow`'s envelope or `monitor-flow`'s `lifecycle_state == "failed"`; reissuing is the loop's call (or the user's `tasks.py` can skip failed entries in `_PRIOR`).
- **`MAX_RUNS` retention.** Long campaigns may bump up against the per-experiment cap (default 500). Set `HPC_MAX_RUNS=<n>` in the env if `campaign-status` starts missing iterations near the start of a long run.
- **Cluster-side queue is out of scope.** Each iteration is a separate `qsub`/`sbatch`. Workloads with thousands of sub-minute tasks may hit scheduler submit-rate limits.
- `campaign-list` and `campaign-status` exit codes: 0 ok, 3 internal.
