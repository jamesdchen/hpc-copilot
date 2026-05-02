Help me run a closed-loop HPC campaign. A campaign is a sequence of `/submit-hpc` invocations that share a `campaign_id` tag; each iteration's `tasks.py` reads `hpc_mapreduce.reduce.history.prior(experiment_dir, campaign_id)` to learn what prior iterations produced and decide what to run next.

The framework is intentionally tiny here: there is no `Strategy` Protocol, no `Context` Protocol, no state file. The user's `tasks.py` does the strategy work using whatever Python library suits — `random`, `optuna`, `nevergrad`, `scikit-optimize`, walk-forward indexing, custom PBT — by import. The framework just maintains the in-flight queue (`hpc_mapreduce.campaign.run_campaign`), tags sidecars (`/submit-hpc --campaign-id`), and reports history (`/campaign-hpc status`).

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

`/campaign-hpc` does not bake the asyncio loop into a CLI subcommand — instead, the user invokes it via Python from inside their experiment repo so they can wire any custom callbacks. Show this template (adapt to the user's submit setup):

```python
import asyncio
from hpc_mapreduce import load_tasks_module, tasks_path
from hpc_mapreduce.campaign import run_campaign
from hpc_mapreduce.reduce.history import prior
from slash_commands import runner, session

CAMPAIGN_ID = "<the slug from Step 1>"
PROFILE = "<your profile name>"   # from a recent run sidecar
CLUSTER = "<your cluster>"
SSH_TARGET = "<user@host>"
REMOTE_PATH = "/u/scratch/.../<exp>"

# `submit_one` and `await_completion` wrap the same /submit-hpc and /monitor-hpc
# pipelines you use today. Build them however your repo prefers — e.g.
# subprocess.run(["hpc-mapreduce", "submit", ...]) or direct Python calls
# into runner.submit_and_record + runner.record_status. The framework
# just needs them to be async callables.

async def submit_one() -> str:
    # ... your per-iteration submit logic ...
    # Must end by writing the per-run sidecar with campaign_id=CAMPAIGN_ID
    # so prior() picks it up next iteration.
    raise NotImplementedError

async def await_completion(run_id: str) -> None:
    # ... poll runner.record_status until the run reaches a terminal state ...
    raise NotImplementedError

def should_submit() -> bool:
    # Re-import tasks.py so the latest _PRIOR is read. total() == 0 stops
    # the loop.
    mod = load_tasks_module(tasks_path("."))
    return mod.total() > 0

result = asyncio.run(run_campaign(
    concurrency=4,                          # K live submits at a time
    submit_one=submit_one,
    await_completion=await_completion,
    should_submit=should_submit,
    on_event=lambda e: print(e, flush=True),
    wall_clock_budget_seconds=86_400,       # optional cap
))
print(result)
```

The asyncio loop maintains *concurrency* live submits, asks `should_submit` whether to launch another, awaits the next-finished one (FIRST_COMPLETED), and repeats until either `should_submit` returns False (the user's `tasks.py` signals termination via `total() == 0`) or the wall-clock budget elapses. Failed iterations land as `on_event` entries with `error`; the loop continues so a single bad iteration doesn't bring down the campaign.

## Step 4: Status / resume

```bash
hpc-mapreduce campaign status --campaign-id <id>
```

Reports per-iteration reduced metrics (oldest-first), in-flight count, and the list of run_ids tagged with this campaign. Use this to:

- See how many iterations have completed and what they produced.
- Decide whether to extend the campaign by re-running Step 3 (the loop will pick up where it left off — `prior()` reads sidecars on disk, no separate state file).
- Investigate failures by feeding individual `run_id`s into `/monitor-hpc` or `/aggregate-hpc`.

Resume after a network drop / laptop sleep: just re-run the Step 3 Python. The asyncio driver re-discovers in-flight runs via `session.find_runs_by_campaign(experiment_dir, CAMPAIGN_ID)`, polls them to terminal state, and continues launching new iterations. Sidecars on disk are the only durable state.

## Step 5: Cleanup

Campaigns share the per-experiment sidecar retention cap (`MAX_RUNS=500` by default; `HPC_MAX_RUNS` env override). Long-running campaigns may bump up against it; raise `HPC_MAX_RUNS` if `prior()` is missing iterations near the start of a long run.

There is no separate `/campaign-hpc delete` — terminating a campaign is just stopping the loop and ignoring the tag from then on. The sidecars remain for future inspection.
