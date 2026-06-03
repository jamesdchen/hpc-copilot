# Closed-loop campaigns

A campaign is a sequence of `/submit` invocations sharing a `campaign_id` tag. The user's `.hpc/tasks.py` reads `hpc_agent.models.mapreduce.reduce.history.prior(experiment_dir, campaign_id)` at module-load time to learn what prior iterations produced and decide what to run next. The framework provides:

| Component | What it does |
|---|---|
| `campaign_id` field on run sidecars (v2 schema) | Tags every successful submit with the campaign it belongs to. |
| `--campaign-id` field on the submit spec | Sets the tag at submit time; threaded through `hpc_agent.ops.submit.runner.submit_and_record` → `RunRecord.campaign_id`. |
| `HPC_CAMPAIGN_ID` env var | Forwarded by every scheduler template (SGE / SLURM, CPU / GPU). The user's `tasks.py` (and the executor) read it on the cluster. |
| `hpc_agent.models.mapreduce.reduce.history.prior(experiment_dir, campaign_id)` | Walks matching sidecars, runs `reduce_metrics` on each iteration's result_dirs, returns the per-iteration reduced-metric dicts oldest-first. Pure local read; no SSH. |
| `hpc_agent.meta.campaign.dirs.campaign_dir(experiment_dir, campaign_id)` | Returns `.hpc/campaigns/<cid>/`, creating it idempotently. Reserved for strategy libraries to put their state files (Optuna SQLite, PBT checkpoints, walk-forward cursor, etc.). The framework writes nothing inside. |
| `hpc_agent.models.mapreduce.metrics_io.read_kw_env()` | Executor-side helper that returns `{lowercase_name: str_value}` for every `HPC_KW_*` env var the dispatcher exported. Stdlib-only; deployed alongside the executor. |
| `hpc-agent campaign status / list` | Read-only CLI inspection. |
| `slash_commands/commands/campaign-hpc.md` | Operator-facing slash that scaffolds a campaign-aware `tasks.py` and arms the loop. The loop itself is driven by `hpc-campaign-driver` (a non-primitive console script) — one step per invocation, advancing off the `delegate` block emitted by `load-context`. Wrap the driver in cron / `/loop` / any external orchestrator; on-disk state is the only thing carried between ticks. Concurrency is opt-in by firing more submits before earlier ones finish. See [`docs/internals/campaign-lifecycle.md`](../internals/campaign-lifecycle.md) for the design rationale and the two prior shapes (`armed-line` Stop hook, conversation-as-state) that this replaced. |

Strategies (Optuna, RandomSearch, walk-forward, PBT, …) are **not** framework citizens. The user picks one by `import`-ing it inside their `tasks.py`. The framework depends on **no optimizer** — not even Optuna. It does ship copy-in *scaffolds* (templates, never imported by the package) for the two non-trivial cases — [`optuna_strategy.py`](../../src/hpc_agent/models/mapreduce/templates/scaffolds/optuna_strategy.py) (scalar ask/tell) and [`pbt_strategy.py`](../../src/hpc_agent/models/mapreduce/templates/scaffolds/pbt_strategy.py) (artifact-carrying clone-and-perturb) — both written cluster-safe (see Recipe 2).

## `tasks.py` recipes

All three recipes share the same bootstrap:

```python
# .hpc/tasks.py — campaign-aware
import os
from hpc_agent.models.mapreduce.reduce.history import prior

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

Requires `pip install optuna` (on both your machine **and** the cluster's
environment). The framework does not depend on Optuna; the user installs it.

**Use the shipped, tested scaffold** rather than hand-rolling this — copy
[`optuna_strategy.py`](../../src/hpc_agent/models/mapreduce/templates/scaffolds/optuna_strategy.py)
to `.hpc/tasks.py` and edit the objective key + search space.

The subtlety the scaffold solves — and why a naive `study.ask()` in
`resolve()` is wrong: **the cluster-side dispatcher imports your `tasks.py`
and calls `resolve(task_id)` on the compute node.** If `resolve()` (or module
scope) calls `study.ask()`, the cluster re-asks against its synced copy of the
study and diverges from what the orchestrator submitted. So a stateful
optimizer must decide params **once, on the orchestrator**, and `resolve()`
must be a deterministic read. The scaffold does exactly that:

- `import optuna` + `study.ask()` live only inside `_propose` (orchestrator);
  the compute node hits the proposal-file fast path and imports no optimizer.
- The per-iteration index is the count of **completed** prior iterations —
  identical on the orchestrator (proposing iteration N: 0..N-1 done) and the
  cluster (running N: 0..N-1 done, N in-flight) — so both read the same
  proposal. `_propose` is idempotent, so the cmd_sha re-import doesn't leak
  trials.
- Reconciliation is by oldest-first index (`prior_records` record `i` ==
  trial `i`); the trial number is also round-tripped as `trial_token`.

Read the iteration history with `prior_records(".", campaign_id)` (the rich
accessor — `{run_id, trial_tokens, result_dirs, metrics, complete}` per
iteration), not the minimal `prior(...)`. See
[`docs/design/campaign-seam.md`](../design/campaign-seam.md).

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

## `cmd_sha` collisions in stochastic strategies — handled by the framework

The framework derives a run's identity (`cmd_sha`) from the SHA-256 of the materialized task list — `[resolve(i) for i in range(total())]`. For a static `tasks.py` this is correct (resubmits dedup automatically). For a **stochastic strategy** it used to be a footgun: if Optuna proposed the same params twice (TPE explores; this happens), the cmd_sha matched a prior trial, the submission deduped, and from Optuna's perspective the trial silently never started.

**The framework now handles this for you.** A campaign-tagged submit no longer dedups against a prior iteration of the *same* campaign: `find_run_by_cmd_sha` skips same-`campaign_id` matches (wired through `submit_and_record`), so a repeated point runs fresh. You do **not** need to inject a unique key into `resolve()` to bust dedup.

If you want to reconcile a finished result back to the proposal that produced it (the concurrent / out-of-order case), return an opaque **`trial_token`** from `resolve()`:

```python
def resolve(i: int) -> dict:
    return {**_next_params, "trial_token": _next.number}  # opaque; framework never reads it
```

`trial_token` is a **reserved key**: it is excluded from `cmd_sha` (see `hpc_agent.state.run_sha.RESERVED_TASK_KEYS`) so it never affects parameter identity, it is still exported to the executor as `HPC_KW_TRIAL_TOKEN`, and it is round-tripped onto the run sidecar and re-surfaced by `prior_records(...)["trial_tokens"]`. For the canonical 1-ask-per-iteration loop you don't even need it — record `i` (oldest-first) corresponds to trial `i`.

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
