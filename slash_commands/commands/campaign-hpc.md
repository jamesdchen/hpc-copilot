Help me run a closed-loop HPC campaign. A campaign is a sequence of `/submit-hpc` invocations that share a `campaign_id` tag; each iteration's `tasks.py` reads `hpc_mapreduce.reduce.history.prior(experiment_dir, campaign_id)` to learn what prior iterations produced and decide what to run next. **The loop is you (the assistant) repeatedly invoking `/submit-hpc`** — not a custom Python driver. `/submit-hpc` already owns the rsync + qsub + sidecar pipeline; `/campaign-hpc` reuses it as-is, threading the `campaign_id` through.

The framework is intentionally tiny here: there is no `Strategy` Protocol, no `Context` Protocol, no state file. The user's `tasks.py` does the strategy work using whatever Python library suits — `random`, `optuna`, `nevergrad`, `scikit-optimize`, walk-forward indexing, custom PBT — by import. The framework just tags sidecars (the `campaign_id` field threads through `/submit-hpc` into every per-run sidecar) and reports history (`/campaign-hpc status`).

CLI shapes for every tool referenced below: see `docs/cli-contract.md`.

## When to use this command

- The user mentions hyperparameter tuning, walk-forward backtesting, active learning, population-based training, adaptive grid refinement, or any pattern where iteration N's submission depends on iteration N-1's results.
- The user has run `/submit-hpc` before and wants to follow up by adapting the next submission to the last result.
- The user explicitly asks to "set up a campaign" or "tag these submissions as part of a study."

If the user just wants one-shot parallel work with no feedback loop, use `/submit-hpc` directly.

## Setup

Read cluster definitions:
- `clusters.yaml`: resolve via `python -c 'from hpc_mapreduce import _PACKAGE_ROOT; print(_PACKAGE_ROOT / "config" / "clusters.yaml")'`

Pick a campaign:

0. **Existing campaign**: Run `hpc-mapreduce campaign list --experiment-dir <cwd>` and read the envelope. If the user references one of the listed `campaign_id`s, jump to Step 4 (resume / status).

1. **New campaign**: Continue to Step 1 below.

## Step 1: Pick a `campaign_id`

Ask the user one question: "What's a short slug for this campaign? (e.g. `ml_ridge_optuna_q1`, `walk_forward_2026q1`)". Constraints:

- Filesystem-safe: matches `^[A-Za-z0-9._\-]+$`.
- Distinct from any existing `campaign_id` in `campaign list` output unless the user is intentionally extending one.

This slug will be threaded through every subsequent submit's sidecar and exported as `HPC_CAMPAIGN_ID` to the cluster.

## Step 2: Confirm or scaffold `.hpc/tasks.py`

A campaign-aware `tasks.py` reads prior iterations' reduced metrics to decide what to run next. **Read [`docs/campaign.md`](../../docs/campaign.md)** (`Read` tool — it's local) for the working code. The "`tasks.py` recipes" section there contains three full patterns plus the shared `_PRIOR` bootstrap; pick whichever matches the user's intent:

- **Recipe 1: Random search** — stdlib only; `random.uniform` over a parameter space; stops after `_MAX_ITER`. Use when the user has no library preference and just wants exploration.
- **Recipe 2: Optuna ask/tell** — requires `pip install optuna` in the user's env (framework does not bundle it). Use when the user mentions Optuna, TPE, or "I want a smart sampler." The doc covers the `_PRIOR`-replay block that backfills Optuna's view after a fresh checkout.
- **Recipe 3: Walk-forward backtesting** — deterministic schedule; iteration N submits window N; no randomness. Use for time-series sweeps where the schedule is fixed up front.

All three share the same `_PRIOR` bootstrap (`prior(".", os.environ["HPC_CAMPAIGN_ID"])` — see doc); copy whichever recipe block fits, then tweak the parameters / search space / window definition with the user.

The chosen pattern fills the body of `.hpc/tasks.py`'s `total()` and `resolve(i)` — same convention as today, just with the prior-reading bootstrap up top.

### Converting an existing `tasks.py` to a campaign

If the user already has a working open-loop `tasks.py` (with `_TASKS = [...]` materialized) and wants to convert it to closed-loop, the minimum diff is two additions:

1. The `_PRIOR` bootstrap (one line — see doc) at the top of the module.
2. A `len(_PRIOR) >= N` stopping check inside `total()` so the campaign loop knows when to exit.

Don't rewrite their `resolve(i)` body without permission — preserving their kwargs shape is what keeps the executor's CLI contract stable.

## Step 3: Run the loop

Two paths, depending on whether the agent should drive each iteration interactively or whether the loop should run programmatically:

### Path A: Interactive iterations (`/submit-hpc` per iteration)

When the user wants to be in the loop — review each iteration's plan, see results, decide whether to continue:

1. **Submit**: invoke `/submit-hpc campaign_id=<slug>`. The slash command's interview + scaffold + smart-planner steps run; ultimately it invokes `submit-flow` (Step 7b–8 of `/submit-hpc`) tagged with `campaign_id`.
2. **Monitor**: `/monitor-hpc <run_id>` until terminal.
3. **Inspect**: `hpc-mapreduce campaign status --campaign-id <slug>` shows per-iteration reduced metrics + in-flight count.
4. **Decide**: re-import `tasks.py`; if `tasks.total() > 0`, go to Step 1. Else done.

### Path B: Programmatic iterations (compose `submit-flow` + `monitor-flow`)

When the user wants the campaign to drive itself — no per-iteration interview, no agent in the per-iteration critical path. This is **true workflow composition**: the campaign loop chains two workflow atoms (`submit-flow` to launch, `monitor-flow` to wait for terminal) per iteration. Both atoms emit JSON envelopes; the loop parses one and feeds the next.

Per iteration:

1. **`hpc-mapreduce submit-flow --spec .hpc/campaigns/<slug>/iter-<N>.submit.json`** — pre-flight + rsync + deploy + qsub + record. Envelope's `data.run_id` and `data.job_ids` flow into the next step.
2. **`hpc-mapreduce monitor-flow --spec .hpc/campaigns/<slug>/iter-<N>.monitor.json`** — internal poll loop until `lifecycle_state` is `complete` / `failed` / `abandoned` / `timeout`. Auto-combines waves as they finish.
3. Strategy reads results — `tasks.py`'s `_PRIOR = prior(...)` picks up the new sidecar at module-load.

```python
import json, subprocess
from pathlib import Path
from hpc_mapreduce import load_tasks_module, tasks_path

def run_one(spec_path, *, verb):
    """Invoke one workflow atom; return parsed envelope or raise."""
    out = subprocess.run(
        ["hpc-mapreduce", verb, "--spec", str(spec_path), "--experiment-dir", "."],
        capture_output=True, text=True, check=False,
    )
    envelope = json.loads(out.stdout.strip().splitlines()[-1])
    if not envelope["ok"]:
        raise RuntimeError(f"{verb} failed: {envelope['error_code']}: {envelope.get('message')}")
    return envelope["data"]

n = 0
while load_tasks_module(tasks_path(".")).total() > 0:
    submit_spec = build_submit_spec(slug, n, base_submit_spec)        # caller helper
    Path(f".hpc/campaigns/{slug}/iter-{n:04d}.submit.json").write_text(json.dumps(submit_spec))
    submit_data = run_one(f".hpc/campaigns/{slug}/iter-{n:04d}.submit.json", verb="submit-flow")
    if submit_data["deduped"]:
        # Replay — original cluster jobs already running. Skip submit, monitor only.
        pass

    monitor_spec = {"run_id": submit_data["run_id"], "poll_interval_seconds": 60}
    Path(f".hpc/campaigns/{slug}/iter-{n:04d}.monitor.json").write_text(json.dumps(monitor_spec))
    monitor_data = run_one(f".hpc/campaigns/{slug}/iter-{n:04d}.monitor.json", verb="monitor-flow")

    if monitor_data["lifecycle_state"] == "failed":
        # MVP monitor-flow doesn't auto-resubmit; campaign decides.
        # tasks.py can choose to skip failed entries in _PRIOR and try
        # the next sample, OR the loop can break here for human triage.
        ...

    n += 1
```

Both atoms emit the same `{"ok": ..., "data": {...}}` shape, so the campaign loop's per-iteration code is a uniform "build spec → invoke atom → parse envelope → branch" pattern. No agent-as-runtime; no slash-command-to-slash-command bridge. The same loop runs in any context — Claude Code conversation, headless cron, external orchestrator (MARs) — because the composition primitives are CLI atoms.

### Concurrency

In either path: invoke another iteration's `submit-flow` before the previous one finishes if you want K-in-flight. The cluster scheduler runs them in parallel. Optuna's `constant_liar=True` and similar mechanisms specifically support this.

### Strategy feedback (telling Optuna / etc. about results)

Two patterns work, pick whichever the user's `tasks.py` is set up for:

- **Tell at module-load** (recommended; idempotent). The next iteration's `tasks.py` re-reads sidecars + per-trial outputs and pushes results into the strategy backend (e.g. `optuna.Study.tell`) before asking for the next batch. No extra orchestration needed — `submit-flow` re-imports `tasks.py` each invocation.
- **Tell between iterations**. After an iteration lands and before the next, run a small helper (`.hpc/campaigns/<slug>/score_iter.py` or similar) that walks per-task outputs and tells the strategy. Use this when the executor doesn't write per-trial reduce JSONs in a shape your `tasks.py` can read on its own.

### Headless overnight runs

Wrap Path B in `/loop` (or a real cron / systemd timer if the user wants to walk away from Claude Code entirely): `/loop 30m bash .hpc/campaigns/<slug>/iterate.sh`. Stops automatically when `tasks.total() == 0`. Path B is the natural fit here because each iteration is one self-contained CLI call — no agent turn required.

## Step 4: Status / resume

```bash
hpc-mapreduce campaign status --campaign-id <id>
```

Reports per-iteration reduced metrics (oldest-first), in-flight count, and the list of run_ids tagged with this campaign. Use this to:

- See how many iterations have completed and what they produced.
- Decide whether to extend the campaign by re-running Step 3 (the loop will pick up where it left off — `prior()` reads sidecars on disk, no separate state file).
- Investigate failures by feeding individual `run_id`s into `/monitor-hpc` or `/aggregate-hpc`.

Resume after a network drop / laptop sleep: there is nothing to "resume" — the loop is just `/submit-hpc` invocations. Run `hpc-mapreduce campaign status --campaign-id <id>` to see what's complete and what's still in-flight, then invoke `/submit-hpc campaign_id=<id>` again to launch the next iteration. `tasks.py`'s `_PRIOR` reflects whatever sidecars are on disk, so the strategy picks up where it left off. Sidecars on disk are the only durable state.

## Step 5: Cleanup

Campaigns share the per-experiment sidecar retention cap (`MAX_RUNS=500` by default; `HPC_MAX_RUNS` env override). Long-running campaigns may bump up against it; raise `HPC_MAX_RUNS` if `prior()` is missing iterations near the start of a long run.

There is no separate `/campaign-hpc delete` — terminating a campaign is just stopping the loop and ignoring the tag from then on. The sidecars remain for future inspection.
