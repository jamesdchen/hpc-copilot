# Campaign Async-Refill + Multi-Cluster — Implementation Plan

**Cold-session handoff.** Self-contained: you can execute this without prior conversation
context. Implements the RFC at [`campaign-async-refill.md`](campaign-async-refill.md) (#362)
plus a multi-cluster capability the RFC does not cover. Read the RFC for the deep rationale;
this plan supersedes its task list where noted (notably §5, already done).

---

## Context

The campaign loop runs `submit → monitor → aggregate → decide` as a **staged barrier**: the
next iteration is proposed only after the current one fully drains. For trials of
heterogeneous duration this idles the pool on stragglers. **Async refill** (RFC #362) keeps
`K` trials in flight, telling results as they land and refilling empty slots.

The motivating real-world case: a hand-rolled `drive_campaign.py` (in a sibling experiment
repo) reimplemented four framework subsystems from scratch — the poll loop, the
tell→ask→resubmit spine, the stop/continue ladder, and a cross-cluster deploy lock — to drive
two clusters (carc + hoffman2) from one repo. Every bug it hit was a re-derivation of
something hpc-agent already does. This plan lands the *framework* versions: async refill, a
correct multi-cluster model, and a real Windows deploy lock.

**RFC status:** proposed, design-complete, **nothing implemented**. The payoff is not
verifiable offline — it has a mandatory live-cluster gate (RFC §10) that cannot be skipped.

---

## Scope decisions (resolved — do not re-litigate)

### 1. Multi-cluster isolation → fix the Windows lock, keep one repo
`infra/io.py` `advisory_flock` is a **no-op on win32** (`fcntl` absent). The existing
per-repo `.submit_lock` (`ops/submit_flow.py:1940`) that serializes concurrent deploys —
preventing the `prune_orphan_sidecars(min_age_seconds=0)` race that drops a sidecar — therefore
**does not actually serialize on Windows**. That is a latent correctness bug affecting *any*
concurrent submit on this platform, not just campaigns.
**Proper fix:** make `advisory_flock` real on win32 (msvcrt), then use the clean
**N-campaign_id, one-repo** model (the `campaign_id` slug is already the isolation primitive;
`find_runs_by_campaign` partitions in-flight sets cleanly). Do **not** sidestep with N
separate repos — that leaves the core bug latent for everyone else.

### 2. Self-driving driver → do NOT build a daemon
Continuous driving **already exists**: `/loop 30m hpc-campaign-driver --experiment-dir .
--allow-agent-steps`. The driver is deliberately one-step-per-invocation, stateless across
ticks, disk-as-truth, harness-agnostic. Two prior in-process self-driving shapes
(conversation-as-state; an armed-line Stop hook) were **ripped out** for breaking
crash-safety (`docs/internals/campaign-lifecycle.md`), and the repo explicitly flags
"driver-invoking-driver" as a recursion hazard. **Building a self-re-exec daemon is the wrong
thing.** The narrow re-arm-on-timeout helper (deferred item #4 in
`docs/workflows/code-driven-orchestration.md`) matters only for a *no-`/loop`* detached mode —
it is an **optional follow-up**, out of scope here.

### 3. Live gate → mechanism + unit tests + a scripted runbook
The RFC is emphatic that unit-green ≠ done. A feature provable only on a cluster, shipped
without a repeatable verifier, never actually gets verified. Deliver: all code fully
unit-tested offline **and** a one-command live-verify runbook (Phase 3) the user runs against
real clusters.

---

## What this plan does NOT build (and why)

- **Self-re-invoking driver / `os.execv` / detached-successor chaining** — cuts against the
  documented crash-safety architecture (decision #2).
- **RFC §5 (trial-tokens wiring)** — **already done.** `ops/resolve_submit_inputs.py:237-244`
  injects both `trial_tokens` and `trial_params` into `sidecar_spec`. Write + read plumbing
  (`compute_run_id` → `write_run_sidecar` → `prior_records`/`history.py:235`) is fully wired;
  only the shipped scaffold doesn't *consume* tokens yet (fixed in 1.5).
- **`total()` returning `B=refill_count`** in the optuna scaffold — superseded by the refill
  granularity decision below.

---

## Design decision: refill granularity (resolves a tension the RFC leaves implicit)

`refill_count` = **number of new iteration submits** needed to bring in-flight up to `K`. The
deterministic resolver loops `_submit_next_iteration` `refill_count` times; each call's fresh
`compute_run_id` re-imports `tasks.py`, which `ask()`s the **next distinct** trial — because
optuna persists a RUNNING trial on `ask()` *before* the next import sees the study, and a
`constant_liar` sampler decorrelates the in-flight asks. This handles **both** granularities
uniformly: generic scaffold (trial == one task) and the chunked case (trial == one array of
chunk-tasks). Therefore the scaffold change is **tell-by-`trial_token` + `constant_liar`**,
**not** `total() == B`.

---

## Phase 0 — Fix `advisory_flock` on Windows (independent; ships first)

- **File:** `src/hpc_agent/infra/io.py` — the win32 branch of `advisory_flock` (~line 264,
  currently a permissions-only no-op).
- **Change:** acquire a real exclusive lock via `msvcrt.locking(fileno, LK_LOCK, 1)` (blocking)
  with a release on context exit, matching the POSIX `fcntl.flock` branch's contract. Mirror
  the proven pattern from the hand-rolled driver's `_try_lock`/`_exclusive`. Keep the existing
  `_replace_with_retry` WinError-5 handling for the atomic-write path.
- **Tests:** `tests/infra/test_atomic_locked_update.py` — add a win32-guarded
  cross-process serialization test. ⚠️ `test_concurrent_writers_serialize` in this file is a
  **pre-existing CI flake** (times out >300s under pytest-timeout); re-run and confirm Phase 0
  doesn't worsen it.
- **Why first:** Phase 2 concurrent cross-cluster deploys depend on it; standalone fix,
  standalone commit.

---

## Phase 1 — Async-refill mechanism (RFC #362 Phase 1), default-OFF

Default path must stay **byte-identical**. Land each sub-step with its own tests.

### 1.1 Manifest fields
- **File:** `src/hpc_agent/_wire/fixtures/campaign_manifest.py` — `CampaignManifest` (top-level
  fields, lines 88-101). Add optional **top-level** fields: `async_refill: bool = False`,
  `max_in_flight: int | None = None`. **Not** under `budget`/`stop_criteria`.
- Regenerate `schemas/campaign_manifest.json` from the model (schema is generated). Backward
  compatible; `manifest_schema_version` stays `1` (added like `circuit_breaker_failures` was).
- **Test:** round-trip; `extra="forbid"` still holds; absent fields default.

### 1.2 `campaign-advance` refill rule
- **File:** `src/hpc_agent/meta/campaign/atoms/advance.py`.
  - Add `_manifest_async_refill` / `_manifest_max_in_flight` helpers — mirror the *structure*
    of `_manifest_circuit_breaker_failures` (lines 279-300, try/except + `isinstance` guard)
    but read **top-level** manifest keys (`manifest.get("async_refill")`), not `stop_criteria`.
  - Add `--async-refill` / `--max-in-flight` to the `CliShape` args tuple (37-84), the function
    signature (92-104), and default-from-manifest (mirror 147-150).
  - Add a `_refill` rule to `rules=[...]` (250-262), ordered **after** `_over_budget` and all
    `stop_*` rules, **replacing `_wait_in_flight` only when `async_refill` is set**. Compute
    `refill_count = max(0, min(K, remaining_max_jobs) - in_flight)` where
    `remaining_max_jobs = budget["remaining"]["max_jobs"]` (**may be `None` = unbounded** — the
    `evidence` dict already carries `status` and `budget`). This folds
    `decide_concurrency.py:90-91` `safe_bound` (currently unused by the driver). Carry
    `refill_count` in `CandidateAction.params`.
  - Surface `decision="refill"` + `refill_count` in the return dict (264-276), the help string
    (31-35), and the docstring ladder (108-115).
- **Test (synthetic evidence):** default-off → never refills (byte-identical);
  async-on + `in_flight < K` + headroom → `refill` with exact count; `over_budget`/`stop_*`
  still win; `wait_in_flight` replaced only when async.

### 1.3 `load-context` async-aware hint (load-bearing)
- **File:** `src/hpc_agent/meta/campaign/atoms/load_context.py`.
  - `_next_step_hint` (def 56; gate 79-85) only emits `decide` when `in_flight == 0`. Change its
    signature to receive the manifest flags (`async_refill`, `max_in_flight`) — thread from the
    caller `load_context` (call site line 358). When async + a campaign exists + **per-campaign**
    `in_flight < K`, return `decide`/`refill` **even while `in_flight > 0`**, while still
    returning `monitor`/`aggregate` for in-flight runs that need them. Per-campaign slot count
    from the `in_flight` rows (they carry `campaign_id`, line 321).
  - `_build_delegate` decide arm (172-196): reuse it for refill (the resolver dispatches on
    `fields.step == "decide"`) or add `step="refill"`. Update hint vocab in the docstrings
    (254-258, 64-68).
- **Test:** `in_flight > 0` + async + free slot → emits decide/refill; default-off unchanged.

### 1.4 Deterministic resolver refill arm
- **File:** `src/hpc_agent/meta/campaign/deterministic_resolver.py`.
  - `_resolve_decide` (193): **before** the `decision != "continue"` terminal gate (252-262),
    add `if decision == "refill":` → loop `_submit_next_iteration` (281) `refill_count` times;
    aggregate the N `tuple[WorkerReport, int]` into ONE report via a new
    `_aggregate_refill_reports` helper (result lists N `run_id`s, decisions deduped, exit-code =
    worst of N, any residue surfaced). Cursor advances once per call (339) → N advances, correct.
  - **Load-bearing dependency on 1.5:** N sequential calls ask N *distinct* trials only because
    `ask()` persists RUNNING before the next `compute_run_id` import + `constant_liar`.
- **Test:** `refill_count=3` → 3 submits, 3 run_ids in merged report, cursor +3; one residue →
  surfaced not swallowed.

### 1.5 Optuna async scaffold variant
- **File:** `src/hpc_agent/execution/mapreduce/templates/scaffolds/optuna_strategy.py`.
  - `_propose` (83-120): tell by `rec["trial_tokens"]` (out-of-order safe) instead of
    oldest-first index (104-108); add a `constant_liar` sampler to `create_study`. **Keep
    `total()` as-is** (granularity decision — do not return `B`).
  - `scaffold-strategy`: emit this async variant when `async_refill` is on (find where
    scaffold-strategy selects templates).
- **Test:** out-of-order landing → correct tells; repeated `ask` within one tick → distinct
  proposals.

### 1.6 LLM-path prose
- **File:** `worker_prompts/campaign.md` — document the refill step; extend the strict
  `decisions` enum only if a new decision point is genuinely needed.

---

## Phase 2 — Multi-cluster (N campaign_ids, one repo)

Most of this **already works** (cid is the isolation primitive; `find_runs_by_campaign`
partitions cleanly; per-cid manifest/cursor are independent). Deliverables:

- **Per-cluster seed blocks:** each cid's manifest `strategy.params` carries that cluster's
  disjoint seed offset; materialized to `HPC_KW_*` via `build_submit_spec`
  (`_campaign_strategy_kw_env`, `incorporation/build/submit_spec.py:73`). Document the pattern.
- **Shared study:** one Optuna storage at a path **outside** any single cid dir (e.g.
  `.hpc/studies/<base>/`), referenced by every cid's `tasks.py`.
- **"One logical campaign" view:** a thin merge over per-cid `campaign-status` — reporting
  only, **no new persisted state**.
- **Driver:** either run N `/loop` drivers (one per cid) or round-robin cids per tick. Safe
  concurrent deploys rely on **Phase 0**.
- **Naming:** `<base>_<clusterkey>` (e.g. `ebm_all_buckets_carc` / `_hoffman2`).
- **Test:** two cids → disjoint in-flight; merge view; concurrent submit serialized by the
  now-working Windows lock.

Key refs: `meta/campaign/dirs.py:campaign_dir`, `state/index.py:find_runs_by_campaign`,
`atoms/status.py:campaign_status`, `infra/clusters.py:load_clusters_config`,
`deterministic_resolver.py:_reconstruct_submit_context` (single-cluster-per-cid by
construction — correct under this model).

---

## Phase 3 — Live-verify harness (RFC §10 gate; the USER runs it)

A script + runbook that checks the four non-skippable acceptance criteria:
1. Pool occupancy stays ≈ `K` across iteration boundaries; utilization measurably higher than
   the synchronous baseline.
2. **Crash-safe resume** — kill the driver mid-tick, restart: **no stranded trials, no
   double-told trials.**
3. Default-off reproduces today's synchronous behavior byte-for-byte.
4. Polling stays within the connection-storm envelope (#346): one `qstat`/login per group.

Not runnable offline; executes against carc/hoffman2.

---

## Verification

- **Per-phase unit tests** as above — all offline, synthetic journal evidence.
- **Pre-commit gate** on changed `.py`: `ruff check --fix`, `ruff format`,
  `mypy --ignore-missing-imports` (run via `python -m mypy`; the `.venv` mypy trampoline is
  broken). Plus targeted `pytest`.
- **Regen:** run `bake_operations_json.py --write` after any `@primitive` change; regen the
  manifest schema after 1.1.
- **Live gate:** Phase 3, user-run. Implementation is "experimental" until it passes.

---

## Implementation gotchas (pulled from code verification)

- `campaign_advance`'s return dict has **no** `refill_count` field today — add it; carry via
  `CandidateAction.params` internally.
- `_next_step_hint` has **no** manifest/`experiment_dir` access today — the signature change
  ripples to call site `load_context.py:358`.
- `_submit_next_iteration` returns **one** `(WorkerReport, int)` and advances the cursor once —
  the N-loop needs an explicit merge helper; don't assume it batches.
- `decide_concurrency` takes a **flat** `remaining_jobs: int`, but `campaign_budget.remaining`
  is a **dict** — select `["max_jobs"]`, which can be `None` (unbounded).
- Manifest fields are **top-level**, not under `stop_criteria` — the two existing manifest
  helpers read `stop_criteria.get(...)`; do not copy that key path.
- The RFC's line citations are mostly accurate but predate the source; its §5 is **stale**
  (already implemented). Re-verify offsets before editing.

## Key files (quick reference)

| Concern | File |
|---|---|
| Windows lock | `src/hpc_agent/infra/io.py` (`advisory_flock`) |
| Manifest schema | `src/hpc_agent/_wire/fixtures/campaign_manifest.py` → `schemas/campaign_manifest.json` |
| Decision ladder | `src/hpc_agent/meta/campaign/atoms/advance.py` |
| Step routing | `src/hpc_agent/meta/campaign/atoms/load_context.py` |
| K-bound (fold in) | `src/hpc_agent/meta/campaign/atoms/decide_concurrency.py` |
| Refill submit loop | `src/hpc_agent/meta/campaign/deterministic_resolver.py` |
| Token round-trip (done) | `src/hpc_agent/ops/resolve_submit_inputs.py`, `.../reduce/history.py` |
| Scaffold | `src/hpc_agent/execution/mapreduce/templates/scaffolds/optuna_strategy.py` |
| Driver (do not daemonize) | `src/hpc_agent/meta/campaign/driver.py`, `_kernel/lifecycle/drive.py` |
| Detached substrate (item #4 follow-up) | `src/hpc_agent/_kernel/lifecycle/detached.py`, `state/journal_poll.py` |
| Multi-cluster namespacing | `meta/campaign/dirs.py`, `state/index.py`, `atoms/status.py` |
