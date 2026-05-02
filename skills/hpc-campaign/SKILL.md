---
name: hpc-campaign
description: "Inspect and run closed-loop campaigns: tagged sequences of /submit invocations whose tasks.py reads prior history to decide what to run next."
allowed-tools: Bash Read Write
---

Closed-loop campaigns let an experiment's `tasks.py` adapt iteration-by-iteration based on prior results. The framework provides three things — a `campaign_id` tag on every submit, the `prior()` history accessor, and an asyncio in-flight queue. Strategies (Optuna, RandomSearch, walk-forward, PBT) live as Python libraries the user imports in their own `tasks.py`. The framework ships **zero** strategy code.

## When to use

- The user mentions hyperparameter tuning, walk-forward backtesting, active learning, population-based training, or any pattern where iteration N's submission depends on iteration N-1's results.
- The user has run `/submit` before and wants to follow up adaptively.

For one-shot parallel work with no feedback loop, use `hpc-submit` directly.

## Inspection

1. List every campaign in this experiment:
   ```bash
   hpc-mapreduce campaign list --experiment-dir <path>
   ```
   Returns `data.campaigns[]` with `campaign_id` and `iterations` per row. Empty list if no tagged sidecars exist yet.

2. Per-iteration history for one campaign:
   ```bash
   hpc-mapreduce campaign status --experiment-dir <path> --campaign-id <id>
   ```
   Returns:
   - `data.campaign_id`, `data.iterations` (count), `data.in_flight` (journal records still in `in_flight` status).
   - `data.history[]` — the per-iteration reduced-metrics dicts, oldest-first. Pending iterations whose result_dirs aren't on disk yet contribute `{}`.
   - `data.run_ids[]` — sidecars matching this campaign, oldest-first. Useful for drilling into a specific iteration via `hpc-mapreduce status --run-id <id>`.

   Both subcommands are JSON-validated against `hpc_mapreduce/schemas/campaign.output.json`.

## Tagging a submit as part of a campaign

Add `"campaign_id": "<slug>"` to the spec passed to `hpc-mapreduce submit`. The slug must match `^[A-Za-z0-9._\-]+$`. The CLI threads it onto `runner.submit_and_record(..., campaign_id=...)` which lands on the journal `RunRecord` and the per-run sidecar (v2 schema). The scheduler templates re-export `HPC_CAMPAIGN_ID` to the cluster; the user's `tasks.py` reads it via `os.environ` and calls `hpc_mapreduce.reduce.history.prior(experiment_dir, campaign_id)` to get the campaign's history.

## Driving the loop

The asyncio loop is `hpc_mapreduce.campaign.run_campaign`. The slash-command surface (`slash_commands/commands/campaign.md`) shows the calling convention; see `docs/campaign.md` for full recipes (random search, Optuna ask/tell, walk-forward) that paste verbatim into a campaign-aware `tasks.py`.

Resume after a network drop or laptop sleep is automatic: re-run the loop and `session.find_runs_by_campaign(experiment_dir, campaign_id)` re-discovers in-flight submits, polls them to terminal state, and continues launching new iterations. Sidecars on disk are the only durable state — there is no separate state file.

## Notes

- **No automatic retry at the campaign level.** A single iteration's failure surfaces via the loop's `on_event` callback with an `error` field; the loop continues. Reissuing failed iterations is the user's `tasks.py` call.
- **`MAX_RUNS` retention.** Long campaigns may bump up against the per-experiment cap (default 500). Set `HPC_MAX_RUNS=<n>` in the env if `prior()` starts missing iterations near the start of a long run.
- **Cluster-side queue is out of scope.** The asyncio loop runs on the login node; each iteration is a separate `qsub`/`sbatch`. Workloads with thousands of sub-minute tasks may hit scheduler submit-rate limits.
- Exit codes for `campaign status` / `campaign list`: 0 ok, 3 internal.
