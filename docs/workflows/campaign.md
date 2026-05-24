# Closed-loop campaigns

A campaign is a sequence of `/submit` invocations sharing a `campaign_id` tag. The user's `.hpc/tasks.py` reads `hpc_agent.mapreduce.reduce.history.prior(experiment_dir, campaign_id)` at module-load time to learn what prior iterations produced and decide what to run next. The framework provides:

| Component | What it does |
|---|---|
| `campaign_id` field on run sidecars (v2 schema) | Tags every successful submit with the campaign it belongs to. |
| `--campaign-id` field on the submit spec | Sets the tag at submit time; threaded through `hpc_agent.ops.submit.runner.submit_and_record` → `RunRecord.campaign_id`. |
| `HPC_CAMPAIGN_ID` env var | Forwarded by every scheduler template (SGE / SLURM, CPU / GPU). The user's `tasks.py` (and the executor) read it on the cluster. |
| `hpc_agent.mapreduce.reduce.history.prior(experiment_dir, campaign_id)` | Walks matching sidecars, runs `reduce_metrics` on each iteration's result_dirs, returns the per-iteration reduced-metric dicts oldest-first. Pure local read; no SSH. |
| `hpc_agent.meta.campaign.dirs.campaign_dir(experiment_dir, campaign_id)` | Returns `.hpc/campaigns/<cid>/`, creating it idempotently. Reserved for strategy libraries to put their state files (Optuna SQLite, PBT checkpoints, walk-forward cursor, etc.). The framework writes nothing inside. |
| `hpc_agent.mapreduce.metrics_io.read_kw_env()` | Executor-side helper that returns `{lowercase_name: str_value}` for every `HPC_KW_*` env var the dispatcher exported. Stdlib-only; deployed alongside the executor. |
| `hpc-agent campaign status / list` | Read-only CLI inspection. |
| `slash_commands/commands/campaign-hpc.md` | Operator-facing slash that scaffolds a campaign-aware `tasks.py` and arms the loop. The loop itself is driven by `hpc-campaign-driver` (a non-primitive console script) — one step per invocation, advancing off the `delegate` block emitted by `load-context`. Wrap the driver in cron / `/loop` / any external orchestrator; on-disk state is the only thing carried between ticks. Concurrency is opt-in by firing more submits before earlier ones finish. See [`docs/internals/campaign-lifecycle.md`](../internals/campaign-lifecycle.md) for the design rationale and the two prior shapes (`armed-line` Stop hook, conversation-as-state) that this replaced. |

Strategies (Optuna, RandomSearch, walk-forward, PBT, …) are **not** framework citizens. The user picks one by `import`-ing it inside their `tasks.py`. The framework ships zero strategy code — not even Optuna.

## `tasks.py` recipes

All three recipes share the same bootstrap:

```python
# .hpc/tasks.py — campaign-aware
import os
from hpc_agent.mapreduce.reduce.history import prior

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
for prior_entry, run_id in zip(_PRIOR, [s["run_id"] for s in __import__("hpc_agent").mapreduce.reduce.history.find_sidecars_by_campaign(".", os.environ.get("HPC_CAMPAIGN_ID", ""))]):
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

## Driving iterations

The "loop" is just repeated `/submit-hpc campaign_id=<slug>` invocations from the slash-command surface; the assistant (or the user) is the loop driver. Per iteration:

1. `/submit-hpc campaign_id=<slug>` — re-imports `tasks.py`, which reads `prior(...)` and asks the strategy library for the next batch.
2. `/monitor-hpc <run_id>` — wait for the iteration to land.
3. Optional: a tiny `score_iter.py`-style helper pushes the just-landed results into the strategy backend (e.g. `optuna.Study.tell`). For strategies whose `tasks.py` re-reads results at module-load (the recommended pattern), this is unnecessary — the next `/submit-hpc` does it automatically.
4. Repeat until `tasks.total() == 0`.

For K-in-flight concurrency, fire `/submit-hpc` again before the previous iteration lands; the cluster scheduler runs them in parallel. Optuna's `constant_liar=True` and similar mechanisms specifically support the "ask while previous trials haven't told yet" case.

For headless overnight runs, wrap the iteration in `/loop`: `/loop 30m /submit-hpc campaign_id=<slug>`. The loop stops automatically when `tasks.total() == 0`.

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

The leading underscore is convention so the executor knows it's framework-bookkeeping and can ignore it. The value also lands in the sidecar's `extra` dict (or `extra.optuna_params` if you prefer), which a post-iteration `score_iter`-style helper can read back to resolve `run_id → trial_number`.

This is a doc convention, not a framework change — there's nothing strategy-specific the framework can do here without breaking the experiment-agnostic property.

## CLI inspection

```bash
# List every known campaign and its iteration count.
hpc-agent campaign list --experiment-dir .

# Per-iteration reduced metrics for one campaign (oldest-first).
hpc-agent campaign status --campaign-id ml_ridge_optuna_q1 --experiment-dir .
```

Both subcommands emit JSON envelopes following `docs/reference/cli-spec.md`; the data block is pinned by `hpc_agent/schemas/campaign.output.json`.

## Resume semantics

There is no driver state to recover — sidecars on disk are the only durable state. If the laptop sleeps, the network drops, or you walk away mid-campaign:

1. Cluster jobs already submitted continue running on the cluster.
2. Sidecars on disk (`.hpc/runs/<run_id>.json`) and the journal (`~/.claude/hpc/<repo_hash>/runs/<run_id>.json`) keep their `campaign_id` tag.
3. To resume: run `hpc-agent campaign status --campaign-id <id>` to see what's complete and what's still in-flight, then invoke `/submit-hpc campaign_id=<id>` again. `tasks.py`'s `_PRIOR = prior(".", HPC_CAMPAIGN_ID)` reflects whatever sidecars are on disk, so the strategy picks up where it left off.

Strategy libraries that need richer state (Optuna's `JournalFileStorage`, PBT's population checkpoints) keep that state wherever they like — typically `.hpc/campaigns/<cid>/` (see `campaign_dir`).

## Failure semantics

A single iteration's failure shows up in the `/submit-hpc` envelope and as a `failed` lifecycle on the per-run sidecar. Reissuing is the assistant's or user's call:

- For tuning strategies, the user's `tasks.py` may choose to skip failed entries in `_PRIOR` and treat the next iteration as a fresh sample.
- For walk-forward, the user may choose to retry the same window manually via `/submit-hpc campaign_id=<slug>`.

The framework deliberately ships no automatic retry policy at the campaign level. `cmd_failures`'s per-task auto-retry (with caps from `runner.DEFAULT_AUTO_RETRY_POLICY`) operates within a single run sidecar and is orthogonal.

## Patterns out of scope

| Pattern | Why deferred |
|---|---|
| **Cluster-side queue** (one array job draining a shared-FS task queue) | Requires reliable `flock` on the shared FS and a cluster-side dispatcher daemon. Login-node K-in-flight covers most workloads; revisit when sub-minute tasks × thousands of pool entries × Lustre/GPFS appears in practice. |
| **Per-campaign retention** | All sidecars share the per-experiment `MAX_RUNS` cap. Long campaigns can bump `HPC_MAX_RUNS` as a workaround. Per-campaign retention is future work. |
