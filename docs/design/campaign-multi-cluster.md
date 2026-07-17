---
status: plan
---
# Design: multi-cluster campaigns (N campaign_ids, one repo)

> **Status:** design-complete. Companion to the async-refill RFC
> [`campaign-async-refill.md`](campaign-async-refill.md) (#362) and its
> [implementation plan](history/campaign-async-refill-implementation-plan.md) (Phase 2).
> Most of the mechanism **already works** — `campaign_id` is the isolation
> primitive and the cross-run queries already partition on it. This document
> names the pattern, the conventions that make it safe, and the one correctness
> dependency (the Windows advisory lock, landed in Phase 0).
>
> Pinned by `tests/meta/campaign/test_multi_cluster_isolation.py`.

## 1. The model: one repo, N campaign_ids — never N repos

To drive two clusters (e.g. CARC + Hoffman2) from a single experiment repo, run
**one campaign_id per cluster in the same tree**. Do **not** clone the repo N
times. The temptation to use N repos comes from wanting isolation; but the
`campaign_id` slug *is* the isolation primitive, and it already gives clean
isolation without duplicating the tree (and without leaving the underlying
Windows-lock bug latent for everyone else — see §6).

Every piece of per-campaign state is keyed by `campaign_id`:

| State | Keyed location | Owner |
|---|---|---|
| Campaign scratch dir | `.hpc/campaigns/<cid>/` | `meta/campaign/dirs.py::campaign_dir` |
| Manifest (opt-in / budget / strategy params) | `.hpc/campaigns/<cid>/manifest.json` | `meta/campaign/manifest.py` |
| Cursor (iteration counter, audit-only) | `.hpc/campaigns/<cid>/cursor.json` | `meta/campaign/cursor.py` |
| In-flight set / run partition | journal records filtered by `campaign_id` | `state/index.py::find_runs_by_campaign` |
| Per-iteration history | sidecars filtered by `campaign_id` | `meta/campaign/atoms/status.py::campaign_status` |

Because `find_runs_by_campaign(experiment_dir, cid)`
(`state/index.py::find_runs_by_campaign`) returns *only* runs whose
`record.campaign_id == cid`, two cids in the same repo partition cleanly: each
cluster's driver sees its own in-flight set and never the other's.
`campaign_status(...).in_flight` (`meta/campaign/atoms/status.py::campaign_status`) counts that
partition, so per-cid in-flight counts are independent. This is the whole basis
of the model, and the test asserts it directly.

`ops/campaign_refill.py::_build_iteration_resolve_spec` reinforces
single-cluster-per-cid: when it rebuilds the next iteration's submit context it
**prefers the most recent run of this campaign** (`find_runs_by_campaign(...)[-1]`),
so each cid rebuilds against its own cluster / profile / remote
path. One cid is one cluster *by construction* — the resolver never has to pick
between clusters, because a cid only ever has runs on one.

## 2. Naming: `<base>_<clusterkey>`

Name each per-cluster campaign `<base>_<clusterkey>`:

```
ebm_all_buckets_carc
ebm_all_buckets_hoffman2
```

- `<base>` is the logical experiment ("one logical campaign", §5) — also the key
  for the **shared study** (§4).
- `<clusterkey>` is the `clusters.yaml` key
  (`infra/clusters.py::load_clusters_config`), so the slug names exactly
  which cluster the cid deploys to and reads its env-activation from.

`campaign_dir` rejects path separators (`dirs.py::campaign_dir`), so the underscore-joined
slug is always a single safe directory name under `.hpc/campaigns/`.

## 3. Per-cluster seed blocks

Each cluster must explore a **disjoint** region of the search space, or the two
drivers waste budget re-evaluating the same points. The mechanism is the
campaign manifest's `strategy.params`, materialized to `HPC_KW_*` environment
variables at submit time.

`build_submit_spec` (`incorporation/build/submit_spec.py::build_submit_spec`) calls
`_campaign_strategy_kw_env(experiment_dir, campaign_id)`
(`submit_spec.py::_campaign_strategy_kw_env`), which reads `manifest.strategy.params` and emits
`{"HPC_KW_<KEY.upper()>": str(value), ...}`. Those land in **both** (a) the
local process env *before* task enumeration imports `tasks.py` — so the local
`cmd_sha` is computed under the right knobs — **and** (b) the cluster `job_env`, so the
running job carries them too. A Path-B strategy `tasks.py` reads its knobs from
`os.environ["HPC_KW_<PARAM>"]`.

### Worked example

Give each cid a disjoint seed offset so their RNG / trial seeds never collide:

```python
# carc cid manifest
write_manifest(
    experiment_dir,
    campaign_id="ebm_all_buckets_carc",
    strategy={"name": "optuna-tpe", "params": {"seed_offset": 0, "seeds_per_tick": 1000}},
)
# hoffman2 cid manifest
write_manifest(
    experiment_dir,
    campaign_id="ebm_all_buckets_hoffman2",
    strategy={"name": "optuna-tpe", "params": {"seed_offset": 1000, "seeds_per_tick": 1000}},
)
```

At submit, the carc job runs with `HPC_KW_SEED_OFFSET=0`,
`HPC_KW_SEEDS_PER_TICK=1000`; the hoffman2 job with
`HPC_KW_SEED_OFFSET=1000`. The user's `tasks.py` seeds each trial as
`seed_offset + i`, so carc draws `[0, 1000)` and hoffman2 draws `[1000, 2000)` —
disjoint blocks, no double-evaluation. (A non-campaign submit, or a manifest with
no `strategy.params`, emits an empty dict — byte-identical to before.)

## 4. Shared study (one Optuna storage, outside any cid dir)

The seed blocks (§3) keep the two clusters from *proposing* the same point. To
also let them **learn from each other** — a result landing on carc should inform
hoffman2's next ask — every cid's `tasks.py` points at **one** Optuna storage,
placed *outside* any single cid's directory:

```
.hpc/studies/<base>/optuna.db          # shared by every <base>_<clusterkey> cid
```

NOT `.hpc/campaigns/<cid>/optuna.db` — that path
(`meta/campaign/dirs.py::campaign_dir`, the conventional per-cid strategy-state
location) is per-cid and would give each cluster its own study. For a multi-
cluster campaign the study must straddle the cids, so it lives one level up under
a `<base>`-keyed path that no cid owns. Every cid's `tasks.py` opens the same
storage URL, so all clusters `ask`/`tell` against one shared sampler state.

(With async refill on, a `constant_liar` sampler — RFC §4 — decorrelates the
concurrent in-flight asks *across* clusters too, since they share the study: a
RUNNING trial one cluster registers on `ask()` steers the other cluster's next
proposal away from it.)

```
.hpc/
├── studies/ebm_all_buckets/optuna.db          ← shared study (no cid owns it)
└── campaigns/
    ├── ebm_all_buckets_carc/{manifest,cursor}.json
    └── ebm_all_buckets_hoffman2/{manifest,cursor}.json
```

## 5. The "one logical campaign" view (reporting only)

A reader often wants a single rolled-up picture across both clusters. That is a
**thin merge over per-cid `campaign-status`** — a reporting-only aggregation,
**with no new persisted state and no new primitive**. Compute it on demand from
the two sources of truth and throw it away:

```python
def merge_campaign_statuses(statuses: list[dict]) -> dict:
    return {
        "iterations": sum(s["iterations"] for s in statuses),
        "in_flight": sum(s["in_flight"] for s in statuses),
        "run_ids": sorted({rid for s in statuses for rid in s["run_ids"]}),
    }

merged = merge_campaign_statuses([
    campaign_status(experiment_dir=exp, campaign_id="ebm_all_buckets_carc"),
    campaign_status(experiment_dir=exp, campaign_id="ebm_all_buckets_hoffman2"),
])
```

There is deliberately **no** persisted "merged campaign": no
`.hpc/campaigns/<base>/` dir, no merged cursor, no merged manifest. The clean
partition (§1) guarantees the unioned `run_ids` are disjoint, so the merged
counts equal the sum of the parts with no de-dup ambiguity. The test asserts the
merge arithmetic *and* that no synthesized `<base>` campaign dir is ever
created.

## 6. Driver & concurrent-deploy safety (Phase 0 dependency)

Two safe driver shapes — both pure functions over disk state, no new daemon
(the deliberate one-step-per-tick, stateless-across-ticks contract;
`docs/internals/campaign-lifecycle.md`):

1. **N `/loop` drivers** — one per cid, e.g.
   `/loop 30m hpc-block-drive --experiment-dir . --campaign-id ebm_all_buckets_carc`
   alongside a second loop for `_hoffman2`. They run concurrently.
2. **Round-robin** — one driver that advances each cid in turn per tick.

Either way, two ticks can land **concurrent submits/deploys into the same repo**.
The per-repo `.submit_lock` exists to serialize those deploys and prevent the
`prune_orphan_sidecars(min_age_seconds=0)` race (`state/runs.py::prune_orphan_sidecars`)
from dropping a sidecar mid-deploy. That lock is `infra/io.py::advisory_flock`.

**This is the one correctness dependency, and it is now fixed.** The win32 branch
of `advisory_flock` was historically a permissions-only **no-op** (`fcntl` absent
on native Windows), so the `.submit_lock` did not actually serialize concurrent
deploys on Windows — the race was unguarded on the one platform without `flock`.
Commit `12043d0d` replaced that branch with a real `msvcrt` byte-range lock (see
`infra/io.py::advisory_flock` for the implementation), giving genuine
cross-process exclusion. Safe concurrent cross-cluster deploys rely on this.

The related `infra/io.py::atomic_locked_update` (which takes its exclusion through
`advisory_flock`) had a Windows correctness gap — it was entirely lockless on win32
— that is now CLOSED: `1f368163` made it take a real lock and `d8130044` outsourced
locking to the `filelock` library (see the `atomic_locked_update` docstring).
Cross-process serialization of
`advisory_flock` itself is proven by
`tests/infra/test_atomic_locked_update.py::test_advisory_flock_serializes_cross_process_win32`.
The multi-cluster test does not duplicate it; it pins only the exclusion contract
(a held lock refuses a second non-blocking acquirer) the concurrent-deploy story
rests on.

## 7. Connection to async-refill

Multi-cluster and async-refill are **orthogonal and composable**. Each cid is an
independent campaign with its own manifest, so each can independently be **async
or sync** — set `async_refill` / `max_in_flight` per cid
(`meta/campaign/manifest.py`). A common shape: both cids async with a per-cluster
`max_in_flight` sized to that cluster's pool, sharing one study (§4) so a result
landing on either cluster refills *and* informs the next ask on both. See
[`campaign-async-refill.md`](campaign-async-refill.md) for the refill mechanism;
nothing in it conflicts with the per-cid partition here.

## 8. Key code references

| Concern | Reference |
|---|---|
| Per-cid scratch dir | `meta/campaign/dirs.py::campaign_dir` |
| Run partition by cid | `state/index.py::find_runs_by_campaign` |
| Per-cid status / in-flight count | `meta/campaign/atoms/status.py::campaign_status` |
| Strategy params → `HPC_KW_*` | `incorporation/build/submit_spec.py::_campaign_strategy_kw_env` |
| Cluster config / env-activation | `infra/clusters.py::load_clusters_config` |
| Single-cluster-per-cid rebuild | `ops/campaign_refill.py::_build_iteration_resolve_spec` |
| Manifest (per-cid opt-in / params) | `meta/campaign/manifest.py::write_manifest` |
| Windows deploy lock (fixed in `12043d0d`) | `infra/io.py::advisory_flock` (win32 branch) |
| Driver (do not daemonize) | `meta/campaign/blocks.py` (campaign reconcile), `_kernel/lifecycle/drive.py` |
