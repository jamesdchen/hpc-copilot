---
name: hpc-campaign
description: "Inspect and run closed-loop campaigns: tagged sequences of submit-flow → monitor-flow → aggregate-flow whose tasks.py reads prior history to decide what to run next."
allowed-tools: Bash Read Write
---

Closed-loop campaigns let an experiment's `tasks.py` adapt iteration-by-iteration based on prior results. The framework provides two things — a `campaign_id` tag on every submit (carried by [submit-flow](../../docs/primitives/submit-flow.md)) and the [campaign-status](../../docs/primitives/campaign-status.md) accessor (called from inside `tasks.py`). The "loop" is repeated `submit-flow → monitor-flow → aggregate-flow` triplets sharing the same `campaign_id` slug — workflow-atom composition with no agent in the per-iteration critical path. Strategies (Optuna, RandomSearch, walk-forward, PBT) live as Python libraries the user imports in their own `tasks.py`. The framework ships **zero** strategy code.

## Step 0: Load context (run this first, every time)

Run `hpc-agent load-context --experiment-dir .` and treat its `data` as the ONLY source of truth for campaign state. Never rely on conversational memory or shell variables — a context compaction, a network drop, or a session restart erases them; the on-disk state does not. This is what makes campaign resume trivial.

- `data.campaigns` — every campaign id, its `iterations_submitted`, and `cursor_iteration`.
- `data.in_flight` — runs still active for this campaign (run_id, stage, job_ids).
- `data.latest_run` — config snapshot (cluster, profile, resources) of the newest iteration.
- `data.next_step_hint` — `submit` / `monitor` / `aggregate` for the current iteration.
- `data.delegate` — the next step as a delegable unit of work. `kind: "cli"` is a deterministic step (`monitor` / `aggregate`) — run the matching workflow atom directly. `kind: "agent"` is a judgement step (a new submission, a `decide`) — hand `delegate.prompt` to a fresh-context subagent. Delegating each step to a fresh context keeps this orchestrator's context from accumulating verbose per-step output across a long campaign.

If a value you need is absent here, derive it from the run sidecar on disk — never from memory.

For unattended runs, the `hpc-campaign-driver` console script (equivalently `python -m hpc_agent.campaign.driver`) advances exactly one step per invocation off the same `delegate` block — `kind: "cli"` steps run directly, `kind: "agent"` steps shell `claude -p` only with `--allow-agent-steps`. Wrap it in cron or `/loop` to walk the campaign; on-disk state is the only thing carried between ticks.

## When to use

- The user mentions hyperparameter tuning, walk-forward backtesting, active learning, population-based training, or any pattern where iteration N's submission depends on iteration N-1's results.
- The user has run `submit-flow` (directly or via `hpc-submit`) before and wants to follow up adaptively.

For one-shot parallel work with no feedback loop, use `hpc-submit` directly.

## Two paths: manual vs strategy-driven

Closed-loop campaigns split along whether `tasks.py` chooses parameters by hand or via a Python optimization library:

- **Path A — manual params**: `tasks.py` enumerates a fixed grid; iteration N submits exactly the entries the user specified. Use when the experimenter knows the search space upfront (small grid, walk-forward windows, ablations).
- **Path B — strategy-driven**: `tasks.py` imports Optuna / RandomSearch / a custom optimizer; calls `prior(experiment_dir, campaign_id)` to read previous iterations' metrics, calls `study.tell(...)` for each one, then `study.ask()` for the next batch. Use when the search space is large and the experimenter wants adaptive sampling.

The framework doesn't care which path the user picks — `tasks.py`'s `total()` + `resolve(task_id)` is the only contract. Both paths thread `campaign_id` through `submit-flow`; both read history via the same `campaign-status` primitive (or its Python form, `hpc_agent.mapreduce.reduce.history.prior`).

## Stochastic-marker gate (Path B only) — MANDATORY before each submit

Strategies that re-sample the same param ranges across iterations (Optuna, random-search, PBT) MUST include a unique iteration-disambiguating field in `tasks.resolve()` so each iteration's `cmd_sha` differs even when the strategy happens to pick repeat params. Idiomatic: a `_optuna_trial_number` (or equivalent) integer in the kwargs dict. Without this, two iterations with identical params would compute the same `cmd_sha`, and the second one would dedupe at submit time — collapsing the campaign into a single iteration silently.

This is **not** advisory. Before every Path B submit (Step 1 of "Driving the loop"), you MUST run the `validate-campaign` gate below; an `error`-severity finding from the `validate-stochastic-marker` validator means `overall: "fail"` and you may NOT proceed to `submit-flow`. Fix the `tasks.py` and re-run.

For Path A (manual params), this isn't needed: the param tuple itself differs per iteration.

### Running the gate

Compute the about-to-submit run's `cmd_sha` (the same value `build-submit-spec` produces), then invoke `validate-campaign` with both `campaign_id` and `expected_cmd_sha` set so the stochastic-marker validator fires:

```bash
hpc-agent validate-campaign --spec validate_campaign.input.json --experiment-dir .
```

Spec must include `{"campaign_id": "<slug>", "expected_cmd_sha": "<cmd_sha>", ...}`. Branch on `data.overall`:

- `pass` / `warn` → proceed to `submit-flow`.
- `fail` → STOP. The `validate-stochastic-marker` finding (`code: stochastic_marker_missing`) lists the prior iteration whose `cmd_sha` collides. Apply its `suggested_fix` — add `_optuna_trial_number` (or equivalent) to `tasks.resolve()`'s output — and re-run the gate. There is no `--force`; the dedup would be silent, so the gate is hard by design.

## Inspection

1. **List every campaign** in this experiment: invoke [campaign-list](../../docs/primitives/campaign-list.md). Empty list if no tagged sidecars exist yet.

2. **Per-iteration history** for one campaign: invoke [campaign-status](../../docs/primitives/campaign-status.md) with `--campaign-id <id>`. The primitive returns iteration count, in-flight count, oldest-first per-iteration reduced metrics (pending iterations contribute `{}`), and the run_ids tagged with this campaign.

## Tagging a submission as part of a campaign

Pass `campaign_id: "<slug>"` in the [submit-flow](../../docs/primitives/submit-flow.md) spec. The slug must match `^[A-Za-z0-9._\-]+$`. The atom threads it onto the per-run sidecar (v2 schema) and the scheduler templates re-export `HPC_CAMPAIGN_ID` to the cluster; the user's `tasks.py` reads it via `os.environ` and calls [campaign-status](../../docs/primitives/campaign-status.md) to get the campaign's history before deciding what to run next.

## Driving the loop

Per iteration, three workflow-atom invocations:

1. **Submit**: for a Path B campaign, FIRST run the mandatory stochastic-marker gate (see "Stochastic-marker gate" above) — `validate-campaign` with `campaign_id` + `expected_cmd_sha`; a `fail` blocks the submit. Then invoke [submit-flow](../../docs/primitives/submit-flow.md) with `campaign_id` set. `tasks.py` is re-imported during scaffolding, so `_PRIOR = prior(".", os.environ["HPC_CAMPAIGN_ID"])` sees every previously-completed iteration before deciding what to submit.
2. **Monitor**: invoke [monitor-flow](../../docs/primitives/monitor-flow.md) with the returned `run_id`. Polls until terminal or budget elapses; returns `lifecycle_state` ∈ `{complete, failed, abandoned, timeout}`.
3. **Aggregate** (optional, when the strategy needs cross-wave reduced metrics): invoke [aggregate-flow](../../docs/primitives/aggregate-flow.md). For per-trial-QLIKE-style strategies, this is where the metric the strategy will `tell()` comes from; for simpler strategies that read per-task reduce JSONs directly, skip this step.
4. **Decide**: re-import `tasks.py` and check `tasks.total()`. If `> 0`, go to Step 1. Else done.

Three CLI calls per iteration, all emitting the same JSON envelope shape. The same loop runs identically under Claude Code, cron, or any external orchestrator because composition is at the CLI-atom level.

Concurrency is opt-in: invoke `submit-flow` again before the previous iteration's `monitor-flow` returns if you want K iterations in flight (Optuna's `constant_liar=True` is built for this). Default to sequential when in doubt.

For headless overnight runs, do not hand-write a loop script. Wrap the driver in a recurring trigger (e.g. `/loop 30m hpc-campaign-driver --experiment-dir . --allow-agent-steps`) — each tick advances exactly one step off the on-disk `delegate` block, and `tasks.total() == 0` ends the campaign automatically (the driver finds nothing in flight and emits a `submit` hint with no work).

Resume after a network drop or laptop sleep is trivial: there is no driver state to recover. Re-run `campaign-status` to see what landed, then resume the loop. Sidecars on disk are the only durable state.

## Notes

- **No automatic retry at the campaign level.** A single iteration's failure surfaces in `submit-flow`'s envelope or `monitor-flow`'s `lifecycle_state == "failed"`; reissuing is the loop's call (or the user's `tasks.py` can skip failed entries in `_PRIOR`).
- **`MAX_RUNS` retention.** Long campaigns may bump up against the per-experiment cap (default 500). Set `HPC_MAX_RUNS=<n>` in the env if `campaign-status` starts missing iterations near the start of a long run.
- **Cluster-side queue is out of scope.** Each iteration is a separate `qsub`/`sbatch`. Workloads with thousands of sub-minute tasks may hit scheduler submit-rate limits.
- `campaign-list` and `campaign-status` exit codes: 0 ok, 3 internal.
