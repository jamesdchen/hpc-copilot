# Fallback inventory

A complete sweep of `src/hpc_agent/` for every site where code **degrades to an
alternate path instead of failing**. The interesting output is the SILENT list
(§4) — ranked by cost-when-wrong — and the one enforcement idea (§6) that would
keep new silent fallbacks from landing unnoticed.

This page is **descriptive** (the engineering-principles posture): it records a
point-in-time audit (2026-07-09) and names the sites by `path::symbol`, never by
line number, so it does not rot against edits that don't move a symbol.

## 1. The anatomy, and the bar

The run-#11 8.4 GB re-ship bug had a fixed anatomy:

> capability probe fails → **silent** fallback to a cruder path → cost paid
> somewhere slow/remote → symptom reads as "it's just slow."

`transport.rsync_push` had exactly this shape (no `rsync` on PATH → tar full-copy
→ NO delta → the whole tree re-ships to CARC over a ~1 MB/s VPN → looks hung).
The fix was not to remove the fallback — the fallback is correct — but to make it
**say so at degrade time** (`transport._disclose_no_rsync`). That is the
counter-doctrine this inventory measures every site against:

**DISCLOSURE-OR-REFUSAL at every fallback.** A degrade that changes cost,
correctness, or freshness must either announce itself (with the reason, at the
moment it degrades) or refuse with a remedy. Silence is the defect.

Each site is classified against this bar:

| Class | Meaning |
|---|---|
| **DISCLOSES** | Says it degraded **and why**, at degrade time (a stderr WARN, an honest `(cached: …)` detail, an in-band `errors[]`/`source=fallback` marker). |
| **REFUSES** | Raises with an actionable remedy rather than degrading. |
| **SILENT** | The zombie class: degrades with no announcement and no in-band marker naming the reason. Ranked in §4. |
| **JUSTIFIED-SILENT** | Silence is the *designed* fail-safe (a broken lease reading "not armed", a fail-open probe that must never wedge, a cache whose miss re-does the safe work). Each is justified inline below. |

## 2. Inventory

### Exemplars — DISCLOSES

| Site | Trigger | Fallback | Cost when wrong | Class |
|---|---|---|---|---|
| `infra/transport.rsync_push` (+ `_disclose_no_rsync` / `_disclose_delta_mode` / `_disclose_payload`) | `_have_rsync()` false (no rsync on PATH, typically native Windows) | `tar c \| ssh tar x`; either a content-hash **delta** (remote hashes its own tree) or a full copy | Full re-ship of an identical tree (the 8.4 GB incident) | **DISCLOSES** — stderr WARN names the NO-DELTA cost, the payload size, AND the reason (first deploy / pre-delta runtime / `HPC_NO_DEPLOY_DELTA=1`). The reference implementation. |
| `ops/preflight/check.check_preflight` (`file_transfer_on_path` check) | `rsync` absent | `scp`+`tar` pair | preflight would falsely fail on a working Windows host | **DISCLOSES** — the check detail names "rsync not found; scp/tar fallback available (…)". |
| `ops/preflight/probe_cache.load_fresh` | fresh SUCCESS verdict within TTL | replay the cached checks, skip the ssh round-trip | a stale "green" hides a host that broke since the probe | **DISCLOSES** — every replayed check carries `(cached: probe passed Ns ago)`; breaker-invalidated + SUCCESS-only + short TTL. |
| `ops/harness_capabilities.harness_capabilities` | a capture hook / relay hook / elicitation server not installed | reports `present=false` and names the **exact friction tier** the absence degrades to | a gate silently downgrades (e.g. authorship falls to agent-authored journal fields) | **DISCLOSES** — the item-12 "declared-but-dark" precedent: detection-as-negotiation, `trusted_display="unknown"` is an honest non-answer, not an asserted `true`. |
| `infra/gpu.pick_gpu` | `_run_qstat` returns `None` (qstat unreachable/absent) | static `preferred[0]` | scheduling on a busy/wrong queue → the job queues longer | **DISCLOSES (in-band, weak)** — return carries `source="fallback"` + `errors=[{code:"qstat_unavailable"}]`; but `gpus[0]` reads identical to a live pick, so a consumer that ignores `errors[]` never sees it. Also appears in §4 for the weak-signal reason. |

### Exemplars — REFUSES

| Site | Trigger | Behavior | Class |
|---|---|---|---|
| `infra/runtime_preflight.runtime_uv_preflight` | `HPC_RUNTIME=uv` but `command -v uv` fails after activation | raises `SpecInvalid` with the install remedy — turns "all 100 tasks fail identically" into one preflight error | **REFUSES** |
| `ops/monitor/reconcile._ssh_list_combined_waves` / `_ssh_alive_job_ids` | ssh transport failure (rc 255) vs. genuinely-empty result | the remote commands append `\|\| true` so a reachable cluster returns rc 0 for "nothing there"; **rc 255 raises `RemoteCommandFailed`** | **REFUSES** — the counter-precedent to the conda-run-blindness class: a connectivity blip can never masquerade as "no waves / nothing alive" and mark a healthy run abandoned. |
| `infra/gpu.load_gpu_config_for_cluster` / `_excluded_prefixes_for_cluster` | malformed `gpu_queues` shape in `clusters.yaml` | raises `SpecInvalid` — "fails loudly rather than silently swapping in the default" | **REFUSES** |

### JUSTIFIED-SILENT

| Site | Trigger | Silent fallback | Why justified |
|---|---|---|---|
| `ops/recover/net_triage` (`read_breaker_state`, control probe) | missing/corrupt breaker state file | reads as "missing" (healthy); TCP probe skipped when the circuit is open | Fail-open is the design: triage must never *write* or burn the breaker's single half-open probe slot; a broken local read must not wedge a read-only differential. |
| `infra/env_flags.env_actor` | `HPC_ACTOR` set but not a valid slug | returns `None` (unattributed, single-actor tier) | Documented: an invalid actor config degrades to today's default tier, not an error — same fail-open posture as utterance capture. |
| `ops/monitor/reconcile._gather_failure_features` | ssh blip fetching the failed-task log tail | empty tail + `None` classification | Evidence *enrichment* only; the `failed` verdict already stands on the reporter's positive `failed>=1` count. Never gates the verdict. |
| `infra/transport.deploy_runtime` (content-hash cache) | manifest read `\|\| true` returns empty / corrupt | full deploy (re-ship all framework files) | Safe direction: a miss re-does the cheap work; a stale manifest can't claim a file landed because the transfer raises *before* the manifest is recorded. (But see §4 S5 — the cache *hit* is silent, an asymmetry with `rsync_push`'s delta disclosure.) |
| `infra/inspect/_common._CACHE` (60s TTL) and the per-invocation probe caches | cache within horizon | serve cached snapshot | Planning-only, 60s horizon, bounded LRU; a stale node score is corrected on the next horizon and never gates a hard step (staging + canary are the gates). |
| `infra/ssh_engine.engine_ssh_run` → `EngineUnavailable` | engine disabled / `asyncssh` unimportable / breaker-refused / wedged | caller falls back to the one-shot ssh path | Opt-in (`HPC_SSH_ENGINE=asyncssh`) and correctness-preserving: the fallback is never *worse* than the default path, and a remote non-zero exit is never mistaken for engine trouble. The fallback itself is transparent — acceptable because the two paths are behaviorally identical. |

## 3. Coverage note

The sweep covered: `except`-and-swallow blocks (≈301 `except → pass/return-default/continue`
sites across 127 files, plus ≈87 `contextlib.suppress` sites), every `shutil.which`
probe (`infra/transport._have_rsync`, `ops/preflight/check`, `ops/recover/notify`),
the `HPC_*` env kill-switches (§5), the scheduler-probe empty-output class
(`infra/inspect/_common._CommandRunner`, `ops/monitor/reconcile`), and the cache
family (`state/canary_cache`, `state/{describe,discover,evidence,preflight,draft_context}_cache`,
`ops/preflight/probe_cache`, `infra/inspect/_common._CACHE`). The overwhelming
majority of the `except-pass` sites are legitimately fail-open (JSON-tolerant
reads, teardown, best-effort telemetry) and are **not** capability-degrade
fallbacks — which is exactly why a blanket allowlist over them is the wrong
enforcement shape (§6).

## 4. The SILENT list (ranked by cost-when-wrong)

| # | Site | Trigger → silent fallback | Cost when wrong | Remediation shape |
|---|---|---|---|---|
| **S1** | `ops/submit_flow._should_run_canary` (#249 cache arm) → `state/canary_cache.is_canary_validated_fresh` | same `cmd_sha` validated within the 4h TTL → **skip the 1-task canary**, submit the full array | The key is `(cmd_sha, version, cluster)` — **not** env-activation. If the cluster env drifted (a `module` update, a broken conda env) inside the TTL, the smoke test that would have caught it is skipped and **every task in the array fails identically** — the exact "cost paid at scale" anatomy. The result carries `canary_done=False`, but that is indistinguishable from a `canary=false` opt-out and names no reason/age. | **Disclosure line** at the skip: `canary skipped: cmd_sha <sha8> validated 37m ago on <cluster> (HPC_NO_CANARY_SKIP=1 to force)` — surfaced in `SubmitFlowResult` and relayed. |
| **S2** | `ops/submit_flow._should_run_canary` (#263 tiny-batch arm) | `total_tasks <= threshold` → skip the canary | Lower cost (the tiny array's own first tasks catch a broken executor about as fast), but still silent about *why* no canary ran. | **Disclosure line**: `canary skipped: batch of N ≤ threshold M — first tasks are the canary`. |
| **S3** | `infra/gpu.pick_gpu` | `qstat` unavailable → `preferred[0]` with `source="fallback"` | Correctness is unaffected (any valid GPU), but `gpus[0]` reads byte-identical to a live pick; a consumer using only the top pick never learns the live-occupancy signal was absent → schedules blind onto a possibly-saturated queue. Disclosure exists only in the ignorable `errors[]`. | **Signature row** — promote the fallback marker out of `errors[]` into the top pick itself (`gpus[0].source="fallback"` is present; the *consumers* must render it) or add a one-line WARN at the `qstat_unavailable` branch. |
| **S4** | `infra/inspect/_common._CommandRunner.run` (+ planner `None`-scoring) | ssh/timeout/missing-binary → `(124/127/1, "", stderr)`; partial probe → `ClusterSnapshot` with `None` numeric fields | A *partially* failed probe silently drops to "unknown, do not score against this signal" (`NodeSnapshot` doc), so node ranking shifts without the `errors[]` necessarily naming which signal went dark → the planner may recommend a stressed/busy node. Cost: a slower run, not a wrong result. | **Disclosure line** — when a probe section is missing, add the dropped signal to `ClusterSnapshot.errors[]` with the node+field named, so a degraded ranking is auditable. |
| **S5** | `infra/transport.deploy_runtime` (cache **hit**) | manifest matches → skip unchanged framework files, ship nothing | Safe direction (no correctness/cost risk), but **asymmetric** with `rsync_push`, which discloses its delta (`X/Y files already on the remote by content-hash`). A silent full-cache-hit deploy gives the operator no "0 files shipped, all N cached" confirmation — the same class of "did anything happen?" opacity the delta disclosure closed. | **Disclosure line** for parity: `deploy cache: N/M framework files already current (pkg <version>); shipping K`. |

## 5. Env kill-switches (silent behavior changes)

Every `HPC_*` env var that silently changes a fallback path, and whether the
change is disclosed:

| Env var | Effect | Disclosed? |
|---|---|---|
| `HPC_NO_DEPLOY_DELTA=1` | forces full-copy tar over the content-hash delta | **Yes** — named as the reason in `transport._disclose_no_rsync`. |
| `HPC_NO_CANARY_SKIP=1` | forces the canary even on a cache hit | n/a (it *restores* the safe path) — but the *default* skip it guards is S1/S2, silent. |
| `HPC_NO_DEPLOY_CACHE=1` | full deploy, skip the manifest | No disclosure, but safe direction (re-ships). |
| `HPC_SSH_ENGINE=asyncssh` | opt-in persistent connection; `EngineUnavailable` falls back to one-shot | JUSTIFIED-SILENT (behaviorally identical fallback). |
| `HPC_NO_SSH_MULTIPLEX=1`, `HPC_SSH_NO_BACKOFF=1`, `HPC_NO_{DISCOVER,DESCRIBE,DRAFT_CONTEXT,EVIDENCE,PREFLIGHT}_CACHE=1` | disable a multiplex/backoff/cache optimization | No disclosure; all are safe-direction (do more work, not less) and reported by `_kernel/extension/capabilities` where operator-visible. |

The kill-switches are largely well-behaved: the ones that could hide cost
(`HPC_NO_DEPLOY_DELTA`) disclose; the rest degrade toward *more* work, not toward
silent staleness. The exception worth noting is that `HPC_NO_CANARY_SKIP`'s
default-OFF state is what enables S1/S2's silent skip.

## 6. Mechanization candidates

**Deserve a code seat (a disclosure line / signature row):**

- **S1 (canary #249 skip)** — highest cost-when-wrong in the inventory and a
  direct instance of the run-#11 anatomy (probe skipped → cost at scale). A
  disclosure line naming the `cmd_sha`, age, cluster, and the
  `HPC_NO_CANARY_SKIP` override is a small, self-contained addition to
  `_should_run_canary` / `SubmitFlowResult`.
- **S5 (deploy cache-hit)** — cheap parity fix: mirror `rsync_push`'s delta
  disclosure so a full-cache-hit deploy confirms "0 shipped, all cached".
- **S3 (gpu fallback)** — the marker already exists in-band; the seat is on the
  *consumers* (render `source=="fallback"` in the pick summary), not new state.

**Documented-accepted (JUSTIFIED-SILENT — leave, with this note as the record):**

- S4 (inspect `None`-scoring) — planning-only, self-correcting on the next 60s
  horizon; a disclosure line is *nice-to-have* (auditability) but the cost is
  bounded to a slower run, never a wrong verdict. Accept unless a live incident
  shows a mis-ranked node caused a real stall.
- The entire JUSTIFIED-SILENT table in §2 — each is a designed fail-safe whose
  silence is correct; this page is their record.

## 7. Enforcement idea (proposed, not built)

**Verdict: FEASIBLE if scoped to capability-probe fallbacks; INFEASIBLE as a
blanket `except-pass` allowlist.**

A blanket allowlist over all ≈388 `except → swallow` / `contextlib.suppress`
sites is the obvious idea and the wrong one: the vast majority are legitimate
fail-open reads (tolerant JSON, teardown, telemetry), so the allowlist would be
huge, high-churn, and low-signal — it would train reviewers to rubber-stamp
additions, defeating the point. The engineering-principles bar ("verify a guard
can actually fire") argues against a guard whose fire path is 99% noise.

The tractable shape is a **narrow lint over the capability-probe fallback class
only** — a small, enumerable set (the `shutil.which`-guarded branches, the named
`_disclose_*` helpers, the `EngineUnavailable`/`qstat_unavailable`/manifest-miss
branches — on the order of 15–30 sites, not 388). The rule:

> A branch that degrades a **capability probe** (a `shutil.which` miss, a typed
> `*Unavailable` fallback, a probe returning `None`/empty that routes to a
> cruder path) must either call a central `disclose_fallback(reason=…)` helper
> **or** carry a cited `# JUSTIFIED-SILENT: <reason>` tag.

This reuses the repo's existing **cited-ALLOWLIST** idiom (`scripts/lint_no_raw_ssh.py`,
`scripts/lint_no_blocklisted_commands.py`) — each of which pins its fire path in
`tests/scripts/`. Feasibility hinges on making the *probe* class detectable: the
cleanest handle is to require every capability-degrade fallback to route through
one `disclose_fallback` seam (as `transport` already routes through `_disclose_*`),
then lint that the seam is called (or the `JUSTIFIED-SILENT` tag present) in any
function containing a `shutil.which` / `EngineUnavailable`-except / `_run_qstat`-style
branch. A new silent fallback then fails CI at authoring time with a message
pointing here. The tag-or-disclose choice keeps the JUSTIFIED-SILENT sites legal
without forcing a spurious disclosure onto a genuine fail-safe.

Starting the registry from §2 + §4 of this page (≈20 named sites) makes the
initial allowlist auditable in one review, which is the property the blanket
version can never have.
