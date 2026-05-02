# Closed-loop campaigns

A campaign is a sequence of `/submit` invocations sharing a `campaign_id` tag. The user's `.hpc/tasks.py` reads `hpc_mapreduce.reduce.history.prior(experiment_dir, campaign_id)` at module-load time to learn what prior iterations produced and decide what to run next. The framework provides:

| Component | What it does |
|---|---|
| `campaign_id` field on run sidecars (v2 schema) | Tags every successful submit with the campaign it belongs to. |
| `--campaign-id` field on the submit spec | Sets the tag at submit time; threaded through `runner.submit_and_record` → `RunRecord.campaign_id`. |
| `HPC_CAMPAIGN_ID` env var | Forwarded by every scheduler template (SGE / SLURM, CPU / GPU). The user's `tasks.py` (and the executor) read it on the cluster. |
| `hpc_mapreduce.reduce.history.prior(experiment_dir, campaign_id)` | Walks matching sidecars, runs `reduce_metrics` on each iteration's result_dirs, returns the per-iteration reduced-metric dicts oldest-first. Pure local read; no SSH. |
| `hpc_mapreduce.campaign.campaign_dir(experiment_dir, campaign_id)` | Returns `.hpc/campaigns/<cid>/`, creating it idempotently. Reserved for strategy libraries to put their state files (Optuna SQLite, PBT checkpoints, walk-forward cursor, etc.). The framework writes nothing inside. |
| `hpc_mapreduce.campaign.run_campaign(...)` | Asyncio in-flight queue. Maintains *concurrency* live submits; user-supplied `submit_one`, `await_completion`, `should_submit` callbacks plus optional `on_iteration_done(run_id, status, raw_metrics)` strategy hook. Stops when `should_submit` returns False or a wall-clock budget elapses. |
| `hpc_mapreduce.campaign.defaults` | `tasks_py_total_predicate`, `poll_until_terminal`, `submit_via_cli` — curried-function defaults that wrap the existing CLI so a campaign driver collapses to ~5 lines for the common case. Strategy-blind. |
| `hpc_mapreduce.map.metrics_io.read_kw_env()` | Executor-side helper that returns `{lowercase_name: str_value}` for every `HPC_KW_*` env var the dispatcher exported. Stdlib-only; deployed alongside the executor. |
| `hpc-mapreduce campaign status / list` | Read-only CLI inspection. |
| `slash_commands/commands/campaign.md` | Conversational interview that scaffolds a campaign-aware `tasks.py`. |

Strategies (Optuna, RandomSearch, walk-forward, PBT, …) are **not** framework citizens. The user picks one by `import`-ing it inside their `tasks.py`. The framework ships zero strategy code — not even Optuna.

## `tasks.py` recipes

All three recipes share the same bootstrap:

```python
# .hpc/tasks.py — campaign-aware
import os
from hpc_mapreduce.reduce.history import prior

_PRIOR = prior(".", os.environ["HPC_CAMPAIGN_ID"]) if "HPC_CAMPAIGN_ID" in os.environ else []
```

Open-loop submits (no `HPC_CAMPAIGN_ID`) leave `_PRIOR` as `[]`, so the same `tasks.py` works for one-shot submissions too. The convention is fully backward-compatible.

### Recipe 1: Random search (stdlib only)

```python
import random
from typing import Any

random.seed(42)

_MAX_ITER = 200
_LR_LO, _LR_HI = 1e-5, 1e-1
_LAYERS_LO, _LAYERS_HI = 1, 6
_OPTIMS = ("adam", "sgd")


def _sample() -> dict[str, Any]:
    return {
        "lr":        10 ** random.uniform(*[__import__("math").log10(x) for x in (_LR_LO, _LR_HI)]),
        "n_layers":  random.randint(_LAYERS_LO, _LAYERS_HI),
        "optimizer": random.choice(_OPTIMS),
    }


def total() -> int:
    return 0 if len(_PRIOR) >= _MAX_ITER else 1


def resolve(i: int) -> dict:
    return _sample()
```

Each iteration submits one task with one randomly-sampled hyperparameter combo. After 200 iterations, `total()` returns 0 and the campaign loop exits.

### Recipe 2: Optuna ask/tell

Requires `pip install optuna`. The framework does not depend on Optuna; the user installs it themselves.

```python
import optuna
from typing import Any

_STORAGE = f"sqlite:///{os.path.dirname(os.path.abspath(__file__))}/../campaigns/{os.environ['HPC_CAMPAIGN_ID']}/optuna.db"
_OBJECTIVE_FIELD = "val_loss"
_DIRECTION = "minimize"
_MAX_TRIALS = 200


def _study() -> optuna.Study:
    return optuna.create_study(
        storage=_STORAGE,
        study_name=os.environ["HPC_CAMPAIGN_ID"],
        direction=_DIRECTION,
        load_if_exists=True,
    )


# Replay any prior reduced metrics into the Optuna study so it sees the
# full history before proposing the next trial.
_study_handle = _study()
for prior_entry, run_id in zip(_PRIOR, [s["run_id"] for s in __import__("hpc_mapreduce").reduce.history.find_sidecars_by_campaign(".", os.environ.get("HPC_CAMPAIGN_ID", ""))]):
    if not prior_entry:
        continue
    # Optuna trial numbers come from the study; we replay only metrics
    # whose trial isn't already known.
    pass  # User-specific replay logic — see Optuna docs.


def total() -> int:
    return 0 if len(_PRIOR) >= _MAX_TRIALS else 1


def resolve(i: int) -> dict:
    trial = _study_handle.ask()
    return {**trial.params, "_optuna_trial_number": trial.number}
```

The user's executor must `study.tell(trial_number, value)` after writing its `metrics.json`, or the loop driver must do it via `await_completion`. Pick whichever fits your existing executor convention.

### Recipe 3: Walk-forward backtesting (deterministic schedule)

```python
from datetime import date, timedelta
from typing import Any

_START = date(2026, 1, 1)
_END = date(2026, 12, 31)
_WINDOW = timedelta(weeks=4)
_STRIDE = timedelta(weeks=2)


def _windows() -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    t = _START
    while t + _WINDOW <= _END:
        out.append({"window_start": t.isoformat(), "window_end": (t + _WINDOW).isoformat()})
        t += _STRIDE
    return out


_WINDOWS = _windows()


def total() -> int:
    return 0 if len(_PRIOR) >= len(_WINDOWS) else 1


def resolve(i: int) -> dict:
    return _WINDOWS[len(_PRIOR)]
```

Iteration N submits the Nth window. `total()` returns 0 when every window has been processed. No randomness, no third-party libraries.

## Driver script template

The slash command (`/campaign`) and the docs above show what `tasks.py` does. The other half is the driver — the Python script that calls `run_campaign` with the right callbacks. The framework ships strategy-blind defaults so the driver collapses to roughly five lines:

```python
# .hpc/campaigns/ml_ridge_optuna_q1/run.py
import asyncio
from datetime import datetime, timezone

from hpc_mapreduce import compute_cmd_sha, load_tasks_module, tasks_path
from hpc_mapreduce.campaign import run_campaign
from hpc_mapreduce.campaign.defaults import (
    poll_until_terminal,
    submit_via_cli,
    tasks_py_total_predicate,
)

CAMPAIGN_ID = "ml_ridge_optuna_q1"
PROFILE = "ml_ridge"


def build_spec() -> dict:
    """Construct one iteration's submission spec.

    Re-imports tasks.py so the strategy library (Optuna here) proposes
    fresh params for this iteration. Adapt the cluster / ssh / paths to
    your repo — typically you'd derive these from the most recent
    matching sidecar via find_existing_runs() rather than hardcode.
    """
    mod = load_tasks_module(tasks_path("."))
    cmd_sha = compute_cmd_sha(mod)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_id = f"{CAMPAIGN_ID}-{ts}-{cmd_sha[:8]}"
    return {
        "profile": PROFILE,
        "cluster": "hoffman2",
        "ssh_target": "user@hoffman2.idre.ucla.edu",
        "remote_path": "/u/scratch/u/user/ml_ridge",
        "job_name": PROFILE,
        "run_id": run_id,
        "job_ids": [],            # filled by your real qsub; placeholder OK for journal
        "total_tasks": int(mod.total()),
        "campaign_id": CAMPAIGN_ID,
    }


async def main() -> None:
    result = await run_campaign(
        concurrency=4,
        submit_one=submit_via_cli(build_spec),
        await_completion=poll_until_terminal("."),
        should_submit=tasks_py_total_predicate("."),
        wall_clock_budget_seconds=24 * 3600,
    )
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
```

Custom callbacks are still supported — pass your own `submit_one` / `await_completion` / `should_submit` if your repo's submit pipeline does something the defaults don't (e.g. you SSH the qsub yourself rather than going through the CLI).

### Wiring strategy state via `on_iteration_done`

For strategies whose state lives in their own backend (Optuna's SQLite, PBT's checkpoints), you can wire the strategy's "tell" call as a `run_campaign` callback rather than burying it in the executor:

```python
import optuna
from hpc_mapreduce.campaign import campaign_dir

_DB = campaign_dir(".", CAMPAIGN_ID) / "optuna.db"
_STUDY = optuna.create_study(
    storage=f"sqlite:///{_DB}", study_name=CAMPAIGN_ID,
    direction="minimize", load_if_exists=True,
)


def _trial_number_from_run_id(run_id: str) -> int:
    """Recover the Optuna trial number stashed in the sidecar.
    See the cmd_sha collision section below for why we stash it."""
    from hpc_mapreduce.job.runs import read_run_sidecar
    sidecar = read_run_sidecar(".", run_id)
    return int(sidecar.get("extra", {}).get("optuna_trial_number"))


def tell_optuna(run_id: str, status: str, raw_metrics: dict) -> None:
    trial_no = _trial_number_from_run_id(run_id)
    if status == "failed":
        _STUDY.tell(trial_no, state=optuna.trial.TrialState.FAIL)
    else:
        _STUDY.tell(trial_no, raw_metrics["val_loss"])


# In main():
result = await run_campaign(
    ...,
    on_iteration_done=tell_optuna,
    experiment_dir=".",         # required for the loop to compute raw_metrics
)
```

The framework knows nothing about Optuna here — `on_iteration_done` is just a hook the loop fires once per iteration with `(run_id, status, raw_metrics)`. Strategy libraries decide what to do with it. Walk-forward users ignore the callback. PBT users use it to drop dead population members.

## Avoiding `cmd_sha` collisions in stochastic strategies

The framework derives a run's identity (`cmd_sha`) from the SHA-256 of the materialized task list — `[resolve(i) for i in range(total())]`. This is the right behavior for static `tasks.py` (resubmits dedup automatically), but it's a **footgun for stochastic strategies**: if Optuna proposes the same params twice (TPE explores; this happens), the cmd_sha matches a prior trial, the framework dedups the submission, and from Optuna's perspective the trial silently never starts.

**The fix is one line in `resolve()`**: include a unique-per-iteration value in the returned dict so cmd_sha differs even for identical params.

```python
def resolve(i: int) -> dict:
    return {
        **_NEXT_PARAMS,
        "_optuna_trial_number": _NEXT.number,   # unique per Optuna ask()
    }
```

Strategy-specific naming variants:

- **Optuna:** `_optuna_trial_number` (assigned by `study.ask()`)
- **PBT:** `_population_index`, `_generation`
- **Random search:** `_iteration_index = len(_PRIOR)`
- **Walk-forward:** Already unique (the window itself differs per iteration); no need.

The leading underscore is convention so the executor knows it's framework-bookkeeping and can ignore it. The value also lands in the sidecar's `extra` dict (or `extra.optuna_params` if you prefer), which the `on_iteration_done` callback can read back to resolve `run_id → trial_number`.

This is a doc convention, not a framework change — there's nothing strategy-specific the framework can do here without breaking the experiment-agnostic property.

## CLI inspection

```bash
# List every known campaign and its iteration count.
hpc-mapreduce campaign list --experiment-dir .

# Per-iteration reduced metrics for one campaign (oldest-first).
hpc-mapreduce campaign status --campaign-id ml_ridge_optuna_q1 --experiment-dir .
```

Both subcommands emit JSON envelopes following `docs/cli-spec.md`; the data block is pinned by `hpc_mapreduce/schemas/campaign.output.json`.

## Resume semantics

The campaign loop is a login-node asyncio driver. If the laptop sleeps, the network drops, or you Ctrl-C the loop:

1. Cluster jobs already submitted continue running.
2. Sidecars on disk (`.hpc/runs/<run_id>.json`) and the journal (`~/.claude/hpc/<repo_hash>/runs/<run_id>.json`) keep their `campaign_id` tag.
3. On the next invocation, `session.find_runs_by_campaign(experiment_dir, campaign_id)` re-discovers in-flight runs; the user's `await_completion` polls them to terminal state; new iterations launch when `should_submit` returns True again.

There is **no separate state file**. Sidecars on disk are the only durable state. Strategy libraries that need richer state (Optuna's `JournalFileStorage`, PBT's population checkpoints) keep that state wherever they like — typically `.hpc/campaigns/<cid>/`.

## Failure semantics

A single iteration's failure surfaces via `on_event({"event": "completed", "run_id": ..., "error": "..."})`. The loop continues. Reissuing a failed iteration is the user's call:

- For tuning strategies, the user's `tasks.py` may choose to skip failed entries in `_PRIOR` and treat the next iteration as a fresh sample.
- For walk-forward, the user may choose to retry the same window manually via `/submit --campaign-id ...`.

The framework deliberately ships no automatic retry policy at the campaign level. `cmd_failures`'s per-task auto-retry (with caps from `runner.DEFAULT_AUTO_RETRY_POLICY`) operates within a single run sidecar and is orthogonal.

## Patterns out of scope

| Pattern | Why deferred |
|---|---|
| **Cluster-side queue** (one array job draining a shared-FS task queue) | Requires reliable `flock` on the shared FS and a cluster-side dispatcher daemon. Login-node K-in-flight covers most workloads; revisit when sub-minute tasks × thousands of pool entries × Lustre/GPFS appears in practice. |
| **Cluster-resident campaign driver** | The loop's single point of failure is the user's machine. Moving the driver onto the cluster would require RDB-backed state and a long-running login-node service. Out of scope for v1. |
| **Per-campaign retention** | All sidecars share the per-experiment `MAX_RUNS` cap. Long campaigns can bump `HPC_MAX_RUNS` as a workaround. Per-campaign retention is future work. |
