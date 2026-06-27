Closed-loop campaigns let an experiment's `tasks.py` adapt iteration-by-iteration based on prior results. The framework provides a `campaign_id` tag on every submit (carried by [submit-flow](../../docs/primitives/submit-flow.md)) and the [campaign-status](../../docs/primitives/campaign-status.md) accessor (called from inside `tasks.py`). The "loop" is repeated `submit-flow → monitor-flow → aggregate-flow` triplets sharing the same `campaign_id` slug. Strategies (Optuna, RandomSearch, walk-forward, PBT) live as user-imported Python libraries; the framework ships **zero** strategy code.

## Reporting conventions

Two fields on the worker report carry observations back to the caller — they are NOT interchangeable:

- **`decisions`** is the **strict enumerated record** of which judgement points this workflow reached. For the **campaign** workflow there are exactly six allowed `point` IDs — any other value is rejected by `parse_worker_report`:
  - `path` (backed by `classify-campaign-path` — manual grid vs strategy-driven)
  - `stochastic_marker` (backed by `validate-stochastic-marker`)
  - `decide` (backed by `campaign-advance` — outcome is `continue`, a `stop_*` halt, `wait_in_flight`, or `refill` when continuous-async is on)
  - `convergence` (backed by `campaign-converged`)
  - `budget` (backed by `campaign-budget`)
  - `concurrency` (backed by `decide-concurrency` — how many iterations in flight)

  Each entry is `{point, outcome, why, chosen?, rejected?}` — `outcome` is a short tag (e.g. `missing`, `converged`, `exhausted`). At a **judgement** point (a genuine control-flow branch the deterministic layer could not decide for you — here `path`, `decide`, and `concurrency`), `why` is **required** (`parse_worker_report` rejects an empty one), and you should set `chosen` (the branch taken) and `rejected` (the alternatives you weighed and discarded). At a deterministic point `why` is a free-form one-liner.

- **`anomalies`** is a **free-form multi-line string** for everything else: the finding `code`, the colliding iteration's `cmd_sha`, a `suggested_fix`, raw evidence — anything that isn't one of the six points.

When in doubt, prefer `anomalies`. **Do not invent new `decisions` point IDs** (`stochastic_marker_missing` is the finding *code*, not the point `stochastic_marker`) — the envelope is rejected and the run reports as broken even when the work succeeded.

## Step 0: Load context (run this first, every time)

Run `hpc-agent load-context --experiment-dir .` and treat its `data` as the ONLY source of truth for campaign state. Never rely on conversational memory or shell variables — a context compaction, a network drop, or a session restart erases them; the on-disk state does not. This is what makes campaign resume trivial.

- `data.campaigns` — every campaign id, its `iterations_submitted`, and `cursor_iteration`.
- `data.in_flight` — runs still active for this campaign (run_id, stage, job_ids).
- `data.latest_run` — config snapshot (cluster, profile, resources) of the newest iteration.
- `data.next_step_hint` — `submit` / `monitor` / `aggregate` for the current iteration.
- `data.delegate` — the next step as a delegable unit of work. `kind: "cli"` is a deterministic step (`monitor` / `aggregate`); `kind: "agent"` is a judgement step (a new submission, a `decide`). The `hpc-campaign-driver` console script consumes this block — see below.

If a value you need is absent here, derive it from the run sidecar on disk — never from memory.

The campaign loop is driven by the `hpc-campaign-driver` console script (equivalently `python -m hpc_agent.meta.campaign.driver`), not by an in-session agent orchestrator. It advances exactly one step per invocation off the `delegate` block — `kind: "cli"` steps run the matching workflow atom directly; `kind: "agent"` steps run in a fresh-context worker (code-rendered prompt, no hand-written prose) and require the `--allow-agent-steps` flag, since spawning an LLM is a billable side effect. Wrap the driver in cron or `/loop` to walk the campaign; on-disk state is the only thing carried between ticks.

## When to use

- The user mentions hyperparameter tuning, walk-forward backtesting, active learning, population-based training, or any pattern where iteration N's submission depends on iteration N-1's results.
- The user has run `submit-flow` (directly or via the submit workflow) before and wants to follow up adaptively.

For one-shot parallel work with no feedback loop, use the submit workflow directly.

## Two paths: manual vs strategy-driven

- **Path A — manual params**: `tasks.py` enumerates a fixed grid. Use for small grids, walk-forward windows, ablations.
- **Path B — strategy-driven**: `tasks.py` imports Optuna / RandomSearch / a custom optimizer; calls `prior(experiment_dir, campaign_id)` to read previous iterations' metrics, `study.tell(...)` for each, then `study.ask()` for the next batch. Use for large search spaces with adaptive sampling.

**Don't infer the path by hand — call [classify-campaign-path](../../docs/primitives/classify-campaign-path.md):** `hpc-agent classify-campaign-path --source-path .hpc/tasks.py` returns `{path, decided_by, signals, supports_async_concurrency, candidates}`. On `decided_by="code"` branch on `path` directly. On `decided_by="judgement"` (`path="unclassifiable"`) decide between `candidates` yourself and record a `path` decision with `chosen`/`rejected`/`why`.

Both paths thread `campaign_id` through `submit-flow` and read history via `campaign-status` (Python form: `hpc_agent.execution.mapreduce.reduce.history.prior`).

## Stochastic-marker gate (Path B only) — MANDATORY before each submit

Path B strategies (Optuna, random-search, PBT) MUST include a unique iteration-disambiguating field in `tasks.resolve()` (idiomatic: `_optuna_trial_number`) so each iteration's `cmd_sha` differs even on repeat params. Without it, identical-param iterations dedupe at submit time and the campaign silently collapses.

Before every Path B submit, run the `validate-campaign` gate below; an `error`-severity finding from `validate-stochastic-marker` means `overall: "fail"` and you may NOT proceed to `submit-flow`. The caller fixes `tasks.py` and re-invokes. Path A doesn't need this — param tuples differ per iteration.

### Running the gate

Compute the about-to-submit run's `cmd_sha` (the value `build-submit-spec` produces), then invoke `validate-campaign` with `campaign_id` + `expected_cmd_sha` set:

```bash
hpc-agent validate-campaign --spec validate_campaign.input.json --experiment-dir .
```

Spec must include `{"campaign_id": "<slug>", "expected_cmd_sha": "<cmd_sha>", ...}`. Branch on `data.overall`:

- `pass` / `warn` → proceed to `submit-flow`.
- `fail` → STOP. Record a `stochastic_marker` decision with outcome `missing`, and put the `validate-stochastic-marker` finding (`code: stochastic_marker_missing`), the colliding iteration's `cmd_sha`, and the `suggested_fix` (add `_optuna_trial_number` to `tasks.resolve()`'s output) in `anomalies` / `why`. No `--force` — the gate is hard by design.

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

### One-call fold for a sequential iteration — `campaign-run`

For a single **sequential** iteration you may fold Steps 1-3 (submit → monitor → aggregate) into one [campaign-run](../../docs/primitives/campaign-run.md) call, which runs `submit-pipeline → status-pipeline → aggregate-flow` in code and returns one typed `stage_reached` (`{submit_failed, run_failed, run_timeout, run_abandoned, aggregate_failed, complete}`; `needs_decision=True` only on the failure/timeout stages). It is a convenience, **not** a replacement for the per-step driver: the Path B stochastic-marker gate (Step 1) still runs first, the `decide` step (Step 4) and cursor advance stay yours, and K-in-flight concurrency still needs the per-step atoms (one blocking call can't overlap iterations). See [campaign-run.md](../../docs/primitives/campaign-run.md) for the embedded-spec shape and the per-iteration canary toggle.

Concurrency is opt-in: invoke `submit-flow` again before the previous iteration's `monitor-flow` returns if you want K iterations in flight (Optuna's `constant_liar=True` is built for this). **Don't reason this from scratch — call [decide-concurrency](../../docs/primitives/decide-concurrency.md):** pass `--supports-async` (from `classify-campaign-path`'s `supports_async_concurrency`), `--remaining-jobs` (from `campaign-budget`'s `remaining.max_jobs`), and `--in-flight`. On `decided_by="code"` it resolved `sequential` (no async support, or no headroom) — follow it. On `decided_by="judgement"` it computed the safe `max_in_flight` bound and the only open choice is *how aggressive* within it: pick K ∈ [1, `max_in_flight`] and record a `concurrency` decision with `chosen`/`rejected`/`why`. Default to sequential when in doubt.

**Continuous-async refill (opt-in).** Two optional top-level fields in `<campaign_dir>/manifest.json` switch the loop from synchronous staged barriers to a continuously-refilled pool: `async_refill: true` and `max_in_flight: <K>` (the pool target, integer ≥ 1). Default-off is unchanged — byte-identical to today's drain-between-waves behavior. With it on, `campaign-advance` (the `decide` atom) folds the `decide-concurrency` K-bound in: its `--async-refill` flag (defaulting from the manifest) makes it emit a **`refill`** outcome in place of `wait_in_flight`, carrying `refill_count = max(0, min(K, remaining_max_jobs) - in_flight)` (K defaults to 4 when unset; `remaining_max_jobs` null means unbounded). `refill` is ordered AFTER `over_budget` and every `stop_*` halt, so a converged / over-budget / circuit-broken campaign stops refilling; with the pool already full it returns `wait_in_flight` instead. On a `refill`, `load-context` routes the `decide` step even while runs are in flight (whenever `in_flight < K`), falling back to the synchronous monitor/aggregate routing once every async campaign's pool is full. The driver submits `refill_count` iterations in one tick — each re-importing `tasks.py` for the next distinct trial — advancing the cursor once per submit. Still one decision step per tick, stateless across ticks; no daemon. Record the result under the existing `decide` point (`outcome: refill`, with `chosen`/`why`) — never a new point ID; a seventh point fails `parse_worker_report`. The matching strategy is the async optuna scaffold: `hpc-agent scaffold-strategy --name optuna --async-refill` emits a variant that identifies finished trials by their `trial_token` (out-of-order safe) and uses a `constant_liar` TPE sampler so K concurrent asks are K distinct points.

For headless overnight runs, do not hand-write a loop script. Wrap the driver in a recurring trigger (e.g. `/loop 30m hpc-campaign-driver --experiment-dir . --allow-agent-steps`) — each tick advances exactly one step off the on-disk `delegate` block, and `tasks.total() == 0` ends the campaign automatically (the driver finds nothing in flight and emits a `submit` hint with no work).

Resume after a network drop or laptop sleep is trivial: there is no driver state to recover. Re-run `campaign-status` to see what landed, then resume the loop. Sidecars on disk are the only durable state.

## Notes

- **No automatic retry at the campaign level.** Failures surface in `submit-flow`'s envelope or `monitor-flow`'s `lifecycle_state == "failed"`; reissuing is the loop's call (or `tasks.py` skips failed entries in `_PRIOR`).
- **`MAX_RUNS` retention.** Per-experiment cap defaults to 500. Set `HPC_MAX_RUNS=<n>` if `campaign-status` starts missing early iterations.
- **Cluster-side queue is out of scope.** Each iteration is a separate `qsub`/`sbatch`; thousands of sub-minute tasks may hit scheduler submit-rate limits.
- `campaign-list` and `campaign-status` exit codes: 0 ok, 3 internal.
