---
status: partially-implemented (phase-1 landed; phase-2 live-verify pending)
---
# Design: opt-in continuous-async campaign refill

> **Status:** Phase 1 has **landed** (2026-07-12) on the block-drive
> architecture; the feature is **not yet non-experimental** — the Phase-2
> live-verify gate (§10, driven by `scripts/campaign_async_live_verify.py`)
> has not run on a real cluster. Tracks
> [#362](https://github.com/jamesdchen/hpc-agent/issues/362). The design below
> is preserved as written; **where it diverges from what shipped, the drift log
> at the end of this document is canonical** (§3's `DeterministicCampaignResolver`
> refill arm was re-homed onto the new `campaign-refill` primitive; §5 shipped
> as part of an earlier wave). This document reshapes the crash-safety-critical
> campaign loop, and its payoff is **not verifiable offline** (it needs a real
> cluster campaign). See
> [`docs/internals/campaign-lifecycle.md`](../internals/campaign-lifecycle.md),
> which records that two prior loop shapes were tried and ripped out, and warns
> to remember *why the surface looks the way it does before changing it again.*
> Read that first.

## 1. Problem & the invariant we will not cross

A campaign runs `submit → monitor → aggregate → decide` iterations, each batch
optionally informed by the prior. Today the loop is a **staged barrier**: the
next iteration is proposed only after the current one fully drains. For trials of
**heterogeneous duration**, that wastes utilization on stragglers — the whole
pool idles waiting for the slowest member of a batch before the next batch starts.

Continuous-async refill keeps `K` trials in flight, telling results as they land
and refilling the empty slots — the pool stays ~full.

The non-negotiable invariant (from campaign-lifecycle.md's TL;DR): the driver
(`_kernel/lifecycle/block_drive.py`, the `hpc-block-drive` console script;
`hpc-campaign-driver` was removed in the worker-removal wave — see the drift log)
advances **exactly one step per invocation** and **carries nothing in memory
across ticks** — every byte of resume state lives on disk in `.hpc/`. Driver crash / session restart / machine reboot must not matter. Two
earlier shapes (conversation-as-state; an armed-line Stop hook) were ripped out
for breaking exactly this. **Therefore the refill must be a pure function over
journal state, recomputed from scratch each tick.** And it must be **opt-in** —
default behavior byte-identical.

## 2. The loop reshape

The per-iteration barrier is enforced **upstream**, not in `campaign-advance`:
`load-context._next_step_hint` (`meta/campaign/atoms/load_context.py:79-82`) only
emits a `decide` step when `in_flight == 0`, so the driver never even *reaches*
the advance decision while runs are in flight. `campaign-advance._wait_in_flight`
(`meta/campaign/atoms/advance.py:209-215`) is a redundant second guard.

Async mode interleaves rather than serializes: in the same tick cadence, keep
monitoring in-flight runs **and** refill empty slots. The barrier is replaced, in
async mode only, by a pool-occupancy target.

## 3. Where the decision lives (the only new persisted state)

Everything the refill needs is already reconstructable from `.hpc/` *except* the
opt-in + the K bound. That is the only new persisted state.

- **Manifest** (`schemas/campaign_manifest.json`, strict `additionalProperties:false`):
  add `async_refill: bool` and `max_in_flight: K`. Per the manifest field-mirror
  discipline (`meta/campaign/manifest.py:1-13`) — "only store fields it can
  independently act on; the schema field lands *after* the primitive arg that acts
  on it" — these land together with the `campaign-advance` arg below, never before.

- **`campaign-advance`** (`meta/campaign/atoms/advance.py`): add `--async-refill`
  / `--max-in-flight` CLI args defaulting from the manifest (mirror
  `_manifest_circuit_breaker_failures`, `advance.py:279-300`). Add one ladder rule,
  ordered **after** `over_budget` and the `stop_*` halts and **replacing
  `wait_in_flight` only when `async_refill` is set**: when `in_flight < K` and
  budget headroom remains, return `decision: "refill"` carrying
  `refill_count = max(0, min(K, remaining.max_jobs) - in_flight)`. This is a pure
  function over `campaign_status` (`atoms/status.py:44-45`, the journal-derived
  `in_flight`) + `campaign_budget` (`atoms/budget.py:126`, `remaining`) → fully
  unit-testable over synthetic evidence. `decide-concurrency`
  (`atoms/decide_concurrency.py:90-91,133`) already computes exactly this K bound
  (`max(1, min(k_cap, remaining_jobs - in_flight))`) but is currently **unused by
  the driver** — fold its computation in here rather than duplicating it.

- **`load-context`** (`meta/campaign/atoms/load_context.py`): make `_next_step_hint`
  async-aware. When `async_refill` is set, a campaign exists, and there are empty
  slots (`in_flight < K`), emit a `decide` (refill) step **even while
  `in_flight > 0`** — while STILL routing `monitor`/`aggregate` for the in-flight
  runs so monitoring keeps advancing. It reads K / async-mode from the manifest.
  This is the load-bearing change: without it the driver never reaches the refill
  decision (§2).

- **The refill ACTOR (re-homed — see drift log).** As designed this was a
  `refill` arm on `DeterministicCampaignResolver._resolve_decide` calling
  `_submit_next_iteration` `refill_count` times, with matching prose in a
  campaign worker prompt (`worker_prompts/campaign.md`). Both
  `deterministic_resolver.py` and the campaign worker prompt were **deleted in
  the worker-removal wave**, so the arm was re-homed onto the block-drive
  architecture as a new side-effecting primitive: `ops/campaign_refill.py::campaign_refill`
  (verb `campaign-refill`). It calls `meta/campaign/atoms/advance.py::_refill`
  authoritatively each tick and, when the decision is `refill`, builds and
  detach-submits `refill_count` iterations via `ops/resolve_submit_inputs.py::resolve_submit_inputs`
  (per-slot, sequential — the distinctness constraint) + `ops/campaign_run.py::campaign_run`.
  It is reached deterministically: `meta/campaign/blocks.py::campaign_watch`
  emits the `watching_refill` terminator, and `infra/block_chain.py::SUCCESSORS`
  chains `("campaign-watch","watching_refill") → "campaign-refill"`. No LLM
  judgment is involved; the greenlit manifest is the standing consent.

## 4. Strategy-contract extension (the correctness half)

The mechanism above keeps `K` *iterations* in flight, but for them to be `K`
*distinct* trials the strategy must propose distinctly under concurrency. The
shipped optuna scaffold (`execution/mapreduce/templates/scaffolds/optuna_strategy.py:83-120`)
cannot today:

- `total()` is hardwired to **1** (one ask per iteration);
- `_propose` tells **oldest-first by index** (`for i, rec in enumerate(_history()): study.tell(study.trials[i], ...)`), assuming `record i == trial i` — which breaks the moment results land out of order;
- it does **not** use `constant_liar`, so asking again while trials are untold (in flight) would repeat proposals.

The async-capable variant must: (a) **tell by `trial_token`** (out-of-order),
using `rec["trial_tokens"]` which the seam already round-trips; (b) ask with a
`constant_liar`-style sampler so concurrent untold trials get diverse proposals;
(c) ask **B = refill_count** distinct trials per tick. PBT already batches a whole
generation (`pbt_strategy.py`, `total()` returns `_POP`), so the
`total()`/`resolve(task_id)` contract *can* express a batch — optuna just chose
B=1. `scaffold-strategy` emits the async variant when `async_refill` is on.

## 5. Trial-tokens wiring fix (prerequisite) — DONE

> **Shipped** (earlier wave, verified 2026-07-12). `ops/resolve_submit_inputs.py::resolve_submit_inputs`
> now threads both `trial_tokens` **and** `trial_params` into the `sidecar_spec`
> `model_copy` update; `incorporation/build/compute_run_id.py::compute_run_id`
> surfaces them and `ops/write_run_sidecar.py::write_run_sidecar` round-trips
> them. The description below is retained for the rationale.

Out-of-order tell (§4a) reads `prior_records(...)["trial_tokens"]`
(`execution/mapreduce/reduce/history.py:227`). On the canonical Step-6d CLI path
those tokens are wired (`campaign-seam.md` status). But the **resolve-submit-inputs
composite the campaign resolver uses drops them**: `resolve_submit_inputs.py`
computes `cr["trial_tokens"]` (≈ line 186 via `compute-run-id`) yet builds
`sidecar_spec = spec.sidecar.model_copy(update={"run_id": ..., "cmd_sha": ...})`
(`:230`) — injecting only run_id/cmd_sha, never the freshly-computed tokens. Thread
`cr["trial_tokens"]` into that `sidecar_spec` update. (This is distinct from, and
in addition to, the `_ensure_run_sidecar` synthesized-sidecar fallback that
`campaign-seam.md` already marks "Still deferred".)

## 6. Crash-safety argument

Each tick recomputes the refill from `.hpc/` with no driver memory:

| Refill input | Reconstructed by |
|---|---|
| in-flight count / ids | `find_runs_by_campaign` (`state/index.py:165`) → `campaign_status.in_flight` (`atoms/status.py:44-45`) |
| completed-to-tell + `trial_tokens` | `prior_records` (`reduce/history.py:174`, `complete = bool(dirs)`) |
| budget headroom | `campaign_budget.remaining` (`atoms/budget.py:126`) |
| the durable *told* set | the optuna sqlite store (`<campaign_dir>/optuna.db`), re-derived by replaying completed history; the `RUNNING`-guarded re-tell + the idempotent per-iteration proposal file make a re-tell a no-op |

Kill the driver mid-tick → the next tick reconstructs identical state. This is the
same memory-stateless property the synchronous loop already relies on; the refill
must not add a single driver-side state file (cursor/manifest stay
counter/audit-only, `meta/campaign/cursor.py`, `manifest.py`).

## 7. Pacing (#346 connection-storm hardening)

K-in-flight must not reignite the connection storm. It does not: `batch-status`
(`ops/monitor/batch_status.py:61`) collapses polling to **one `qstat`/login-node
per group regardless of run count**, so more in-flight runs add **no** per-run
poll cost. New submits serialize through `safe_interval`
(`infra/ssh_throttle.py`) when enabled. The `HPC_STATUS_POLL_INTERVAL_SEC` floor
and adaptive backoff (`ops/monitor_flow.py:128,144`) still apply. The envelope
holds.

## 8. Opt-in & default-safety

No `async_refill` in the manifest ⇒ `campaign-advance` returns `wait_in_flight`
and `load-context` gates on `in_flight == 0`, exactly as today. Every new branch
is dead unless the flag is set — a guard that fires only under the opt-in, and is
tested with the flag on (so it is a *fireable* guard, not inert code).

## 9. Phasing

- **Phase 1 — offline-unit-testable, lands as one coherent opt-in unit:** manifest
  fields + the advance `refill` decision + async-aware load-context routing +
  resolver submit-N + the §5 trial-tokens fix + the optuna scaffold async
  `_propose`. Default byte-identical. Tests: the advance decision over synthetic
  `status`/`budget` evidence; the resolver submit-N loop with a mocked `submit_fn`;
  the scaffold ask/tell with a mocked study (out-of-order tell + constant_liar +
  B distinct asks); a property test that the default (flag off) path is unchanged.

- **Phase 2 — the live-verify gate (cannot be skipped):** §10. Phase 1's unit
  tests prove the *mechanism*; only a cluster run proves the *behavior*. The
  feature is not "done" on green unit tests.

## 10. Live-verify protocol (Phase 2 gate)

On a real cluster (Hoffman2/CARC), an optuna campaign with `async_refill` on,
`max_in_flight = 4`, and deliberately heterogeneous trial durations (e.g. one
slow outlier per batch):

1. **Pool occupancy** stays ≈ K across iteration boundaries (no drain-to-zero) —
   measurably higher utilization than the synchronous baseline on the same
   straggler-heavy workload.
2. **Crash-safe resume:** kill the driver (`hpc-block-drive`) mid-stream, restart — it
   reconstructs the in-flight/told sets from `.hpc/` and resumes with **no
   stranded trials and no double-told trials**.
3. **Default unchanged:** the same campaign with the flag off reproduces today's
   synchronous batch behavior byte-for-byte.
4. Polling stays within the connection-storm envelope (one query per login-node
   per poll, regardless of in-flight count).

Only after this gate passes does the implementation land as non-experimental.

## Implementation drift log

Deviations from the design above, each with its recorded reason. Canon for the
implemented shape lives HERE; the sections above are the original design and
`docs/design/history/campaign-async-refill-implementation-plan.md` is the
superseded intermediate plan.

### v0 (earlier waves, pre-2026-07-12) — landed before the actor build

- **The advance ladder shipped** (§3 `campaign-advance` bullet). `meta/campaign/atoms/advance.py`
  carries `--async-refill` / `--max-in-flight`, the `_refill` rule
  (`refill_count = min(K - in_flight, remaining_max_jobs)`), `_drain_before_stop`,
  and manifest defaulting. The `decide-concurrency` K-bound was folded in as
  designed.
- **Manifest fields shipped** (§3 manifest bullet): `async_refill` + `max_in_flight`
  in `schemas/campaign_manifest.json`.
- **Async-aware load-context routing shipped** (§3 load-context bullet):
  `meta/campaign/atoms/load_context.py::_async_should_refill` /
  `_next_step_hint` / `_campaign_async_config` call `campaign-advance`
  authoritatively and route a refill step only on `decision == "refill"`.
- **The async optuna scaffold shipped** (§4): tell-by-`trial_token` +
  `constant_liar` live in
  `execution/mapreduce/templates/scaffolds/optuna_async_strategy.py`;
  `scaffold-strategy` emits it under `--async-refill`.
- **§5 (trial-tokens wiring) shipped** — see the §5 DONE banner.

### v1 (2026-07-12) — the refill ACTOR, re-homed onto block-drive

- **§3's `DeterministicCampaignResolver._resolve_decide` refill arm did NOT
  ship as designed.** `meta/campaign/deterministic_resolver.py` and the campaign
  worker prompt (`worker_prompts/campaign.md`) it depended on were **deleted in
  the worker-removal wave** (the `claude -p` bare-worker spawn transport went
  with them; see `docs/design/history/proving-run-2-hardening.md`). A dangling
  "refill arm" comment in `load_context.py` was the only surviving reference and
  is now repointed.
- **The actor is the new `campaign-refill` primitive**
  (`ops/campaign_refill.py::campaign_refill`, verb `workflow`, agent-facing,
  side-effecting, idempotent per tick). Each tick it calls
  `meta/campaign/atoms/advance.py::_refill` and, on `decision == "refill"`,
  resolves + detach-submits `refill_count` iterations **sequentially** through
  `ops/resolve_submit_inputs.py::resolve_submit_inputs` (the per-slot sidecar
  write is what advances the async scaffold's `_submitted_count`, so each slot
  asks a **distinct** trial — the ordering is load-bearing) + `ops/campaign_run.py::campaign_run`
  with `detach=True`. `campaign-run` is the iteration spine.
- **Reached deterministically, no LLM judgment.** `meta/campaign/blocks.py::campaign_watch`
  gained a fourth no-boundary terminator `watching_refill`, split out of
  `watching_healthy`; `infra/block_chain.py::SUCCESSORS` chains
  `("campaign-watch","watching_refill") → "campaign-refill"`; every
  `campaign-refill` stage maps to `None` so the chain ends and the next tick
  re-enters via `campaign-watch` (one-step-per-tick preserved). When async is on
  and the manifest is greenlit and advance says `refill`,
  `load_context._build_delegate` routes a `kind="cli"` refill step.
- **No new state files, no cursor** (design §3/§6 held). Crash-mid-tick residue
  is bounded: a slot that wrote its sidecar but not yet spawned its
  `campaign-run` child leaves an orphan sidecar that still counts against
  `in_flight`, so next tick's `refill_count` shrinks and the partial tick
  self-corrects; `load-context`/`doctor` surface the orphan. This is exactly the
  window the §10 live-verify "no stranded trials" criterion exercises.
- **Consent model:** the greenlit manifest is the standing consent for
  autonomous refill; iterations carry no per-iteration human boundary
  (human-amplification design §4). `campaign-refill` refuses an un-greenlit
  campaign and is NOT in `GATED_BLOCKS`.

### Still gating

Phase 2 (§10) has **not** run. `scripts/campaign_async_live_verify.py` on a real
cluster is the gate; green unit tests prove the mechanism, not the behavior. The
feature stays experimental until it passes.
