# SUBMIT-ONCE — adversarial premortem (build gate)

Unit U3 · transport-robustness sequence · **DOCS-ONLY** · premortems
`SUBMIT-ONCE-DESIGN.md` before any `src/**` lands · verified against source at
main @ `c893d2fa` · 2026-07-17.

**Verdict: GO-WITH-CHANGES.** Eight binding deltas below, three of them
build-blocking (Δ1, Δ2, Δ3). The design's spine — a shell-written cluster-durable
jobmap, `submitting`-before-dispatch, reconcile-owned recovery, positive-evidence
ladder — is sound and the reader-migration table is accurate. But the design
**leans toward the wrong resolution of OPEN-1** for the wrong reason, **its
OPEN-2 "counter suffices" rests on an atomicity the code does not have**, and it
**omits the deployment-skew prune-deletion that an old co-resident wheel
inflicts**. Each is a duplicate-array or lost-orphan kill-class defect. Resolved,
the contract is buildable and the OPENs collapse into one another cleanly.

The load-bearing synthesis the design is one step away from: **Δ1 (atomic
compare-and-mint) establishes a single-attempt-in-flight invariant; that
invariant is the precondition every other simplification silently assumes**
(OPEN-2's counter, OPEN-4's "cleanup is hygiene", and the *only* world in which
dropping the correlation key could ever be safe). Make it explicit and mechanize
it first.

---

## 1. Four lenses

### (a) Correctness / race

**A1 — HIGH, BLOCKS BUILD · the compare-and-mint is not atomic; the `submitting`
branch does NOT close the concurrent-submit race.** `submit_and_record`
(`ops/submit/runner.py:309`) reads the dedup record at `:420`
(`existing = load_run(...)`) and mints the `RunRecord` at `:634`
(`upsert_run(record)`) with **no run-scoped lock spanning the two** — `load_run`
and `upsert_run` each take their own brief lock, but there is no critical section
across the decision. The design's §3.3 migration row for `_resolve_layer1` and
OPEN-2 both assume "an existing `submitting` record refuses a concurrent
submit." That is true only for a **sequential** retry (B starts after A's mint is
durable). Two genuinely concurrent same-run_id submits both execute `load_run` at
`:420` before either mint lands → both see "no record" → **both `_PROCEED` →
both dispatch → the exact duplicate array the contract exists to prevent**, now
with two racing `submitting` records and a contended `attempt` counter. This is
*pre-existing* (deterministic-run_id + dedup only closes sequential retries) but
the design **inherits it while claiming to close the orphan→duplicate class**,
and OPEN-2 explicitly leans on it being closed. The F47 pre-stamp
(`submit_flow.py:2365`) is a crash-window guard, not a concurrency guard — its
existence is evidence the codebase serializes by determinism, not by locking.
→ **Δ1.**

**A2 — MED · marker records `rc` but the adopt rung ignores it → phantom-id
adopt.** §3.2 step 2 captures `rc=$?` alongside `JID`, but the §3.4 adopt row
("`waves` has id(s), `attempt` matches → ADOPT") never checks it. A `qsub` that
**fails** (rc≠0) can still print to stdout; `JID=$(qsub …)` then captures garbage
and the append writes a non-empty `waves` entry. Reconcile would adopt a job id
that names no live array → a permanently-"in_flight" ghost that `read_announcements`
can never settle. → **Δ4.**

**A3 — MED · canary and main share a run identity family; the jobmap protocol is
specified per-run_id but never disambiguates them.** The canary is a **distinct
run_id** (`<run_id>-canary`, its own sidecar + journal record + F47 guard —
`submit_flow.py:2399` `_refuse_prestamped_canary_without_journal`, job name
`f"{spec.job_name}_canary"` at `:2531`). Keying the jobmap by run_id therefore
gives the canary its own `<canary_run_id>.jobmap` **for free** — good — but the
design's §5 ("canary rides the same contract") never says so, and reconcile's new
`submitting` entry-condition must own canary `submitting` records too. Multi-wave
`submit_plan` is the opposite case: all waves share **one** run_id → **one**
`<run_id>.jobmap` with a `waves` map, and rung-1b disambiguation of the in-flight
wave is a set-difference `(scheduler-by-token ids) − (jobmap.waves ids)`, which
the design gestures at ("adopts landed ids and disambiguates only the in-flight
wave") but does not mechanize. → **Δ5.**

**A4 — LOW · `attempt` must advance on the #276 in-place-redo `_PROCEED` path.**
A resubmittable-terminal corpse re-fires under the same run_id
(`runner.py:451`, `_resolve_layer1 → _PROCEED "terminal_failure_resubmittable"`).
If the redo mints `attempt=N` again, a stale `<run_id>.jobmap` from the prior
attempt (left inert per OPEN-4) would `attempt`-match and be falsely adopted.
`attempt` must derive from `max(record.attempt, jobmap.attempt) + 1` at mint (ties
OPEN-2/OPEN-4). → folded into **Δ1**'s locked mint.

### (b) Doctrine / consent

**Verdict: mostly compliant.** Adoption supplies only the *id binding* from the
marker and then re-derives the lifecycle verdict via `read_announcements`
(`ops/monitor/announce.py:75`) — the marker **informs, never settles** (row 11
honored). The id it adopts is positive evidence: an *acked* read of a value the
dispatching shell durably wrote, not an inference from absence. The jobmap read
inherits the announce ack sentinel verbatim (`announce.py:72` `_ANNOUNCE_ACK`,
`:108` echo-after-`cd`, `:118` absent-ack → raise) — the design's `__HPC_JOBMAP_ACK__`
is a correct clone.

**Two absence-reads to police:**

- **B1 — rung 2 is the only rung that trusts absence** ("marker absent, dir ack
  seen → SAFE RE-SUBMIT"). It is *logically* sound because the pending-write
  **precedes** `qsub` in the same `bash -lc` round-trip, so an acked-absent marker
  proves qsub never ran — **but only if `remote_path` is on a filesystem shared
  across every login node the client may round-robin to.** On a cluster with
  node-local scratch, a marker written on login node A is invisible to a reconcile
  that dials login node B → acked-empty-dir → false "never dispatched" → SAFE
  RE-SUBMIT → **duplicate.** The announce plane already makes this shared-FS
  assumption (`announce.py:103`), so it is inherited, not new — but it must be
  *pinned*, and rung 2 should cross-check the announce dir's absence too before
  trusting "never dispatched." → **Δ6.**
- **B2 — rung 1b clean-miss** trusts absence-in-the-scheduler-queue, correctly
  gated on the query ack (rc==0, the `ssh_batch_scheduler_states` refusal,
  `AUDIT.md` §3b). Sound **iff** the query key is run-unique — which it is not
  under OPEN-1(iii). → **Δ2.**

### (c) Deployment / skew — the critical lens

**C1 — HIGH, BLOCKS BUILD · an OLD co-resident wheel garbage-collects the
`submitting` orphan.** `RunRecord.status` is a plain `str`
(`state/run_record.py:169`), and no consumer coerces it through `JournalStatus(...)`
on the load path (grep: the only enum use is `set(JournalStatus)` inside
`mark_run`, `state/journal.py:398`), so an old wheel **loads** a `submitting`
record without raising — good, that half is safe. The kill is `prune_terminal_runs`
(`state/index.py:353`): its guard is `payload.get("status","in_flight") ==
"in_flight"` → **keep only in_flight, prune everything else**. An old wheel
therefore treats `submitting` as terminal and **unlinks the record** — deleting
the only durable evidence of the orphan the new-wheel reconcile needs. The design
fixes this guard in the *new* wheel (§3.3 row, correctly flagged a latent bug) but
**cannot fix an old wheel already installed in another env on the same journal
home** — and MEMORY records exactly this hazard live (the demo venv and the two
cluster envs routinely lag the client wheel). The journal home is shared by every
tool that runs in that repo (CLI, MCP server, hooks, a lagging daemon). → **Δ3.**

**C2 — cluster wheel version is a non-issue for the marker itself** (worth stating
to bound the blast radius): the jobmap is written by the **remote bash shell**, not
by any Python on the cluster, so the cluster's installed wheel is irrelevant to
marker production. The skew surface is entirely **client-side readers of the local
journal**, which C1 covers. The `mkdir -p .hpc/submit` + temp+`mv` discipline is
plain POSIX sh, version-free.

**C3 — old wheel cannot *promote* a submitting record** (`mark_run` would
`raise ValueError` on `"submitting"`, `journal.py:398`) — but old-wheel code never
tries to promote it (it doesn't know the state), and `find_in_flight_runs`
(`index.py:159,176`) string-compares `== "in_flight"` so it simply omits it (treats
it as not-live — safe). The *only* old-wheel path that touches it destructively is
prune (C1). So Δ3 is the whole skew fix.

### (d) Fault-injection reachability (vs FAULT-HARNESS.md's 18 drills)

The apex drill — **AUDIT §7 row 4, "qsub dispatch→job-id window"** — is listed in
FAULT-HARNESS §4 as a *needed seam*, owner U3, "blocked on a submit-leg injection
seam." So U3 **owns building both the seam and its drills** in the same PR (harness
§5.2). Ladder-rung coverage:

| Ladder rung (§4) | Existing drill it clones | NEW seam / drill U3 must add |
|---|---|---|
| 0 happy + append confirmed | submit path tests | assert the marker `waves` entry landed with rc==0; client parse byte-identical |
| 1 jobmap read severed → stay `submitting` | `test_channel_refusal::test_status_report_rc0_no_ack_raises` (§2 row 5) — **2-line `garble_at` clone** on the new jobmap-read seam | jobmap-read seam + rc-0-no-ack drill |
| 1a marker has id, sever before client parse → **ADOPT, no re-qsub** | — (this is the apex) | **submit-leg injection point**: sever AFTER scheduler-accept, BEFORE stdout reaches client; a `submit_one` call-count spy asserts **zero** re-`qsub`; reconcile adopts from marker |
| 1b marker pending, no id → scheduler-query by token | `test_scheduler_states_rc0_no_ack_is_unreachable` (§2 row 6) for the ack-gate | fake scheduler-query returning the orphaned id → adopt; clean-miss variant → resubmit. **Requires the run-unique correlation key (Δ2) to exist to even be writable** |
| 2 marker absent → safe resubmit attempt+1 | — | sever BEFORE pending-write → assert one fresh dispatch at `attempt+1`; **plus a strict-xfail non-shared-FS false-negative variant** (Δ6) |
| 3 all severed → held + `find_stalled` surfaces | reuse `fake_clock` + `find_stalled_runs` (`index.py:241`) | lapse `next_tick_due`, assert `doctor` surfaces "stuck submitting" |

**Two NEW drills outside the transport-sever family the harness must also carry:**
- **the compare-and-mint race (Δ1):** two threads into `submit_and_record` for one
  run_id → assert exactly one dispatch + one `submitting` record (a
  state-concurrency fire test in the daemon-premortem style, not a `sever_at`).
- **the skew prune (Δ3):** a planted `submitting` record is pruned by **nothing**
  (design §7 names this) — and the reverse regression: an *old-guard* prune
  (`status != "in_flight"`) WOULD delete it, pinned as the exact fire the new guard
  closes.

`timeout -k` remote-half reachability (harness §4 row 15, "remote half") is still
uncovered and is the physical substrate of rungs 1a/1b — see OPEN-5.

---

## 2. Resolving the five OPENs from code facts

### OPEN-1 · Where the name-hash rides — **REJECT (iii); choose (i). The design's
decision criterion is the wrong test.**

The design would drop the correlation key if "the append-killed window is
unreachable." That is the wrong criterion, and (iii) is unsound on two independent
code facts:

1. **`job_name` is not injective on run_id.** It defaults to the scheduler-family
   name: `job_name = job_name or profile` (`incorporation/build/submit_spec.py:508`).
   Dozens of unrelated runs carry `job_name = "slurm"`/`"sge"`. Even when set, two
   runs sharing a `run_name` but differing in swept params get the **same
   `job_name`** but **different run_id** (run_id = run_name + 8-hex cmd_sha, §3.1).
   A rung-1b query keyed on plain `job_name` therefore matches **other runs' live
   jobs** → cross-run **false adopt**. The correlation key *must* carry run_id, and
   `job_name` cannot (SGE ≤15 chars, and it is consumed byte-for-byte by log paths
   `_engine.py:1219-1222` and canary naming `submit_flow.py:2531`).

2. **The unrecoverable window is not "append killed" — it is "qsub killed
   mid-flight."** OPEN-5 shows the append (a sub-ms `mv`) survives (below), so the
   append-killed window the design worried about is indeed near-unreachable. But
   that does **not** retire rung-1b: SIGKILL interrupting the `qsub` **process
   itself** before `JID=$(…)` assigns (the scheduler may already have accepted — the
   apex orphan) leaves the marker `pending` with no id, and **only** a scheduler
   query by a run-unique key can recover it. The design conflates the two windows.

**Resolution:** carry the full `run_id#attempt` token in a length-unconstrained
scheduler **context/comment** field — Slurm `--comment`, SGE `qsub -ac key=val`
context, PBS custom resource — read back via `scontrol show job` / `qstat -j`
(option i). This avoids the name-length wall and the log-path/canary collisions
entirely, and it is run+attempt-unique. The 11-char name-hash (original option a)
is a lossy, collision-prone stand-in for what the comment field carries losslessly.
→ **Δ2.**

### OPEN-2 · `attempt` allocation & durability — **a record-field counter suffices
ONLY WITH Δ1.**

OPEN-2 is correct *conditionally* and the design states the condition ("if
`_resolve_layer1`'s new `submitting` branch refuses a concurrent submit outright")
— but that condition is **false as the code stands** (finding A1: the refuse can't
fire against a submit whose `load_run` predates the mint). So: a simple counter on
the `RunRecord`, **bumped inside the same `_locked(run_path)` critical section that
performs the dedup lookup and the `submitting` mint** (Δ1), suffices and gives a
single-attempt-in-flight invariant. Without that lock, no counter allocation scheme
is safe — two submits mint the same `attempt`. Derive at mint as
`max(record.attempt, jobmap.attempt)+1` (covers the #276 redo, A4).

### OPEN-3 · Wire/schema surface — **project, do not enumerate. No schema value, no
version bump.** *(definitively answered from source.)*

`StatusResult.lifecycle_state` is typed `LifecycleStateObservableWithTimeout`
(`_wire/queries/status.py:23`) and `status.output.json:22` fixes the enum to the
**five `LifecycleState` values** under `additionalProperties: false`. The status
query emits a **projected** `LifecycleState`, never the raw journal `status`. Since
(a) `submitting` is deliberately **not** a `LifecycleState` (§3.3, and
`_kernel/contract/vocabulary.py:75` has no such value) and (b) a `submitting` run
has **no `job_ids`** so the status poll has nothing to compute a lifecycle over, the
status query **never runs on a `submitting` record**. Therefore: **add no schema
value and bump no schema version.** The *only* place `submitting` needs a face is
`status-snapshot`/`doctor` renders (§3.3 last two rows), which read the journal
`status` string directly — add a display projection there ("submitting — dispatch
in flight"). **Never echo the raw journal status into `lifecycle_state`** — it would
violate the `enum`/`additionalProperties:false` contract. The design's OPEN-3
"AUDIT" cell is thus resolved to the no-bump branch. → **Δ8.**

### OPEN-4 · `.hpc/submit/` cleanup — **inert is correct, but only because Δ1 gives
the single-attempt invariant.** The `mkdir -p .hpc/submit` folds cost-free into the
same `_execute_command` string (design is right). Leaving stale jobmaps inert is
safe *iff* `attempt`-discrimination truly guards a future same-run_id run — which
holds **only** under the single-attempt invariant (Δ1) and the A4 `attempt+1`-at-redo
rule. Given both, follow the announce-dir precedent (leave inert). Without them,
inert stale jobmaps are a false-adopt reservoir. Optional terminal-harvest prune is
hygiene, not correctness. → covered by **Δ1**; no separate delta.

### OPEN-5 · Does the append survive `timeout -k` grace — **yes; but this does NOT
license OPEN-1(iii).**

The numbers: `build_remote_command` (`infra/remote.py:248`) wraps as
`timeout -k 10 <deadline>s bash -c '<cmd>'` with the SIGKILL grace fixed at
`_REMOTE_DEADLINE_KILL_GRACE_SEC = 10` (`remote.py:145`); `deadline =
client_budget + REMOTE_DEADLINE_MARGIN_SEC(60)` for the submit leg (≈120s client →
≈180s remote bound), or `REMOTE_DEADLINE_DEFAULT_SEC = 3600` when the client set no
timeout (`remote.py:141,234`). The jobmap append is a single `mv` **rename
syscall** — it does not `fork`, so even under login-node fork exhaustion (run-12
finding-20) it completes well inside the 10s SIGKILL grace; only a **wedged
filesystem** could reap it mid-`mv`. So the append-killed window is
near-unreachable — which is why rung-1b's job is **not** to cover it. Rung-1b covers
the `qsub`-killed-mid-flight window (reachable), and *that* is why the run-unique
correlation key (Δ2) stays load-bearing. The build's step-3 injection at the
`timeout -k` boundary (harness §4 row 15) should assert the append lands under a
planted SIGTERM-then-grace, and separately drive the qsub-mid-flight kill to prove
rung-1b fires.

---

## 3. Verdict, binding deltas, build decomposition

### GO-WITH-CHANGES

The contract is approved for build behind the same telemetry-gated caution as the
rest of the sequence (§6), **conditioned on** the eight deltas. Δ1, Δ2, Δ3 are
build-blocking (each is an independent duplicate-array or lost-orphan path). Δ4–Δ8
are required-at-land.

| # | Binding delta | Blocks build? |
|---|---|---|
| **Δ1** | **Atomic compare-and-mint.** Hold one `_locked(run_path)` across the `_resolve_layer1` dedup lookup (`runner.py:420`), the `attempt` allocation (`max(record.attempt, jobmap.attempt)+1`), and the `submitting` mint. Two concurrent same-run_id submits must yield exactly one dispatch + one `submitting` record. This establishes the single-attempt-in-flight invariant every other simplification assumes. Add a distinct `_resolve_layer1` action (`_RECONCILE`, not `_DEDUP`/`_PROCEED`) for an existing `submitting` record → route to reconcile, refuse a blind resubmit. Fire test: two-thread submit; and a `submitting`-record submit → `_RECONCILE`, not the leaky `_DEDUP`. | **YES** |
| **Δ2** | **Reject OPEN-1(iii); carry `run_id#attempt` in a scheduler context/comment field (option i).** `job_name` defaults to `profile` (`submit_spec.py:508`) and is not injective on run_id, so rung-1b by plain name false-adopts across runs. Query via `scontrol show job` / `qstat -j`. Fire test: two runs with colliding `job_name`, one orphaned → recovery adopts **only** the matching-token job. | **YES** |
| **Δ3** | **Two-phase deployment (reader-tolerance first).** Land + deploy to ALL envs — the `prune_terminal_runs` guard fix (`index.py:353` → "keep `status not in TERMINAL_STATUSES`"), `find_submitting_runs`, the `mark_run` enum extension, the `find_stalled_runs` extend, the status/doctor renders — **before** any wheel mints `submitting`-before-dispatch. Gate the mint behind a capability flag until the box is uniformly new-wheel, or an old co-resident wheel's prune will garbage-collect the orphan. Fire test: planted `submitting` record survives prune under the new guard; the old guard (regression pin) deletes it. | **YES** |
| **Δ4** | **Adopt only on marker `rc==0`.** §3.4 adopt must require the recorded `rc==0` AND `JOB_ID_REGEX.search(JID)` (`backends/__init__.py:580`) matches; an `rc≠0` marker is a **confirmed failed dispatch** → safe-resubmit, never adopt. | at land |
| **Δ5** | **State the canary/multi-wave jobmap keying.** Canary mints its own `<canary_run_id>.jobmap` + `submitting` record (distinct run_id, `submit_flow.py:2531`); reconcile owns canary `submitting` records. Multi-wave uses one `<run_id>.jobmap` with a per-wave `waves` map; rung-1b in-flight-wave discovery = `(scheduler-by-token ids) − (jobmap.waves ids)`. | at land |
| **Δ6** | **Pin the shared-FS precondition and cross-check rung 2.** rung 2 (absent marker → never dispatched) is sound only when `remote_path` is shared across all reachable login nodes (inherited from `announce.py:103`). Require the announce dir to be **also** absent before trusting "never dispatched"; carry a strict-xfail non-shared-FS drill. | at land |
| **Δ7** | **Fold the jobmap statements into BOTH command shapes.** `_execute_command` has a cached-bin **direct** form (`_remote_base.py:260`, the steady-state path) and a **login-shell** form (`:268`); the design cites only `:268`. The append must land in both, and `JID=$(qsub …); printf '%s\n' "$JID"` must reproduce qsub stdout byte-for-byte so `JOB_ID_REGEX` parse is unchanged. | at land |
| **Δ8** | **OPEN-3 resolved: project, no schema bump.** No `submitting` value in `status.output.json`; add a `status-snapshot`/`doctor` display projection only; never echo raw journal status into `lifecycle_state`. | at land |

### Build-unit decomposition (files, order, blast radius)

Ordered by dependency; **Δ3's phase-1 is a standalone prerequisite wave** (ship +
deploy before phase-2 touches the actuation).

1. **U3-a · reader tolerance (Δ3 phase 1)** — `_kernel/contract/vocabulary.py`
   (+`SUBMITTING`, keep out of `TERMINAL_STATUSES`), `state/journal.py`
   (`mark_run` free once enum extends; `is_resubmittable_terminal` unchanged),
   `state/index.py` (`prune_terminal_runs:353` guard fix — **latent-bug fix,
   ship-worthy alone**; `find_submitting_runs` NEW; `find_stalled_runs` extend),
   `ops/monitor/*` + `ops/recover/doctor.py` renders. *Blast radius: state layer +
   render surfaces; no actuation.* Ship, deploy to all three envs, THEN proceed.
2. **U3-b · the submit atom (Δ1, Δ4, Δ7)** — `ops/submit/runner.py` (locked
   compare-and-mint, `submitting`-then-promote ordering, `attempt` counter,
   `_RECONCILE` action), `infra/backends/_remote_base.py` (jobmap statements folded
   into both command shapes), new `ops/submit/jobmap.py` (marker write/read helper +
   `__HPC_JOBMAP_ACK__`, cloned from `announce.py`). *Blast radius: the one
   non-idempotent seam — highest; every array submission funnels through it.*
3. **U3-c · correlation key (Δ2)** — `infra/backends/_engine.py` /
   `profile.py` (emit the context/comment field per scheduler), the qstat-`-j` /
   `scontrol`-by-token query. *Blast radius: per-scheduler command shape; test on all
   three families.*
4. **U3-d · reconcile recovery (Δ5, Δ6)** — `ops/monitor/reconcile.py` (entry
   condition `status=="submitting"`, the §3.4 outcome table, rc-gate, wave
   set-difference, announce cross-check). *Blast radius: reconcile owner.*
5. **U3-e · the fault drills** — `tests/faultinject/` submit-leg seam + the six
   ladder drills + the race + skew fires; move harness §4 row 4 → §2 (covered).
   Land in the **same PR** as U3-b/c/d per harness §5.2.
6. **Wire/render (Δ8)** — no schema change; `ops/status_blocks.py` +
   `ops/recover/doctor.py` display projection only.

### Enforcement row (§7) — amend before mechanization

The design's drafted `transport.submit-once-discover-id` row is correct but must
add three fire-clauses this premortem surfaced, so the row's "fires when" is
complete:

> *Also fires when:* the `submitting` mint is not performed under the same
> `run_lock` critical section as the dedup lookup (Δ1 — concurrent double-dispatch);
> a recovery adopts a marker whose recorded `rc≠0` or whose `JID` fails
> `JOB_ID_REGEX` (Δ4 — phantom-id adopt); rung-2 trusts an acked-absent marker
> without the announce-dir cross-check (Δ6 — non-shared-FS false-negative). *And a
> deployment guard:* the `submitting`-minting code path is reachable while any
> reader on the journal home runs a wheel whose `prune_terminal_runs` predates the
> §3.3 guard fix (Δ3 — old-wheel orphan deletion).

---

## Drift log

- 2026-07-17: **U3-c + U3-d + U3-e built (program complete except the live-wiring flip).**
  U3-c carries the `run_id#attempt` token in a scheduler CONTEXT/COMMENT field
  (Slurm `--comment`, SGE `-ac HPC_TOKEN=`), NEVER `job_name` — OPEN-1 resolved to
  (i) as this doc rejected (iii); the per-family emission
  (`ProfileBackend.build_correlation_flags`) folds into the ONE `_build_command`
  funnel double-gated on `HPC_SUBMIT_ONCE` + a run_id (flag-off byte-identity
  re-pinned), and the rung-1b query (`build_token_query_cmd`/`parse_token_query`,
  ack-gated) reads it back via `squeue -o %k` / `qstat -j`. U3-d makes reconcile
  the SOLE transition-out owner (`_recover_submitting`, entry-conditioned in
  `_reconcile_one`): the §3.4 outcome table with the Δ4 adopt gate (rc==0 AND
  `JOB_ID_REGEX`, else 1b), the Δ6 announce cross-check on rung-2, and the ruled
  safe-resubmit (`submitting→abandoned` + jobmap clear → `attempt+1`). U3-e drilled
  the apex dispatch→id-window (adopt-no-reqsub) + the sever/prune/race/phantom-id
  fires; **O5 confirmed reachable-only as the qsub-killed window, not the append —
  the marker append is a single fork-free `mv`, so the append-killed drill is
  documented unreachable-in-process (`xfail(strict=False)`, harness §4-row-15
  precedent) and rung-1b (the token query) is the load-bearing recovery, exactly
  as this premortem argued.** Δ1–Δ7 all mechanized behind the flag; the lone
  remaining OWED clause is the live `submit_flow` mint-before-dispatch + promote
  wiring (the single final flip). Enforcement row (§7 / `lifecycle-verdicts.md`)
  amended: reconcile-sole-owner + adopt-gate moved OWED→mechanized with the
  `test_correlation_key` / `test_reconcile_submitting` / `test_submit_once` pins.
- 2026-07-17: Created. Premortem of SUBMIT-ONCE-DESIGN.md against source @ `c893d2fa`.
  Verdict GO-WITH-CHANGES, 8 binding deltas (Δ1/Δ2/Δ3 build-blocking). Key catches:
  the compare-and-mint is unlocked (`runner.py:420`→`:634`) so the `submitting`
  branch does not close the concurrent-submit race (A1/OPEN-2); `job_name` defaults
  to `profile` (`submit_spec.py:508`) so OPEN-1(iii) false-adopts across runs
  (rejected → option i); an old co-resident wheel's `prune_terminal_runs:353` deletes
  the `submitting` orphan (C1 → two-phase deploy). OPEN-3 definitively resolved from
  `_wire/queries/status.py:23` + `status.output.json:22`: project, no schema bump.
  OPEN-5 resolved (append survives 10s SIGKILL grace, `remote.py:145`) but shown not
  to license OPEN-1(iii) — the reachable window is qsub-killed, not append-killed.
