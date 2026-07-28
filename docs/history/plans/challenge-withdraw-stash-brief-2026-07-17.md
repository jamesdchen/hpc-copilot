# Ruling brief: `stash@{0}` — the "challenge withdraw" WIP

**Date:** 2026-07-17
**Stash:** `stash@{0}` = commit `6e43deb3` ("WIP on main: b3d0082c fix(decision):
unattributed challenge withdraw refused, routed through challenge-verdict (#37,
RULING 4)")
**HEAD at brief time:** `c8bd0e7e`
**Verdict:** **DROP.** Every line of the stash's content has since landed in
named, more-complete commits. Nothing in it waits on an open ruling. The
"challenge withdraw" in the label is a red herring — the auto-generated stash
subject, not the stash content.

---

## 0. First, kill the label confusion

`git stash` names a WIP after **whatever commit HEAD was on when you stashed** —
here `b3d0082c`, the challenge-withdraw fix. That is the *base*, not the
*content*. The stash **does not touch `journal.py`** (verified:
`git stash show --stat stash@{0} | grep journal` → nothing), which is where the
challenge-withdraw / `challenge_verdict` logic lives. `b3d0082c` itself is a
committed, in-HEAD-ancestry fix (`git merge-base --is-ancestor b3d0082c HEAD` →
yes); it needs nothing from this stash.

**What the stash actually contains** is a mid-flight draft of the run-13
pre-work: the **G4 transport-lifecycle shrink**, the **G9 scheduler-dialect
profiles**, the **G2 host-retarget/settle-run verbs**, and the **finding-29
verify-relay numeric-grammar** cleanup — four unrelated workstreams captured in
one uncommitted snapshot.

---

## 1. What the WIP does, hunk by hunk

Sixteen files, 676 insertions / 233 deletions. Grouped by workstream:

### A. G9 — per-family scheduler dialect profiles
- **`infra/backends/profile.py`** — adds the frozen `FamilyDialect` dataclass
  (`supports_comma_array_ranges`, `cap_style`, `explicit_id_liveness_query`), the
  `FAMILY_DIALECTS` matrix (one entry per `KNOWN_FAMILIES`), and `dialect_for()`
  (loud on an unknown family).
- **`infra/backends/_engine.py`** — deletes the hardcoded
  `_COMMA_ARRAY_FAMILIES` frozenset and the `family == "slurm"` liveness branch;
  routes all three decisions (comma-array grammar, in-array cap style, explicit-id
  liveness verdict) through `dialect_for(...)`. Fixes: #5 (finished PBS runs
  pinned at UNKNOWN because SGE's rc==0 rule leaked onto PBS), #6 (non-contiguous
  resubmit split), #32 (PBS Pro `-l max_run_subjobs=N` vs TORQUE `%N` suffix).
- **`tests/infra/backends/test_concurrency_cap.py`**,
  **`tests/infra/test_scheduler_states.py`** — split the PBS-Pro/TORQUE cap and
  liveness cases apart; pin the divergence.

### B. G4 — SSH engine liveness shrink (delegate to asyncssh)
- **`infra/ssh_engine.py`** — removes the hand-rolled `_reap_if_idle` framework
  liveness reaper; adds `_keepalive_interval()` / `_DEFAULT_KEEPALIVE_INTERVAL` /
  `_KEEPALIVE_COUNT_MAX` so death detection is asyncssh-native; reworks the reuse
  path into a 3-attempt get→mark-busy loop under the guard so an in-flight command
  (`inflight > 0`) can never be recycled mid-command (finding-24 no-mid-command-
  sever). `IDLE_CLOSE_SEC` is re-cast as a slot/session *courtesy* recycle, not a
  liveness timer.
- **`tests/infra/test_ssh_engine.py`** — replaces `test_idle_connection_is_reaped`
  with reuse-not-reaped + a `TestNativeKeepaliveLifecycle` class (kwargs carry
  keepalives, shared knob, death surfaces at use time via library exception).

### C. G4 — SSH slot TTL removal
- **`infra/ssh_slots.py`** — deletes `SLOT_TTL_SEC` and every wall-clock branch;
  `_claim_is_stale` loses its `now` param and becomes **pid-liveness only**. A dead
  holder's slot is reaped on pid-liveness; a live holder's slot is never stolen on
  age alone (the #35 over-admission cause). Ownership-bound `release_slot` becomes
  the primary hand-off. Disclosure line switches `ttl:` → `pid(alive/DEAD)`.
- **`tests/infra/test_ssh_slots.py`** — drops the TTL import + TTL-reclaim tests;
  adds "live pid never reclaimed by wall clock" and re-frames #35 around pid-reuse.

### D. G4 — detached lease-lock bounding
- **`_kernel/lifecycle/detached.py`** — adds `_LEASE_LOCK_TIMEOUT_SEC = 60.0`,
  `_read_lease_holder_pid`, and a bounded `advisory_flock` acquire that raises
  `DetachedLeaseHeld` **naming the wedged holder's pid** on expiry (run-#12 finding
  16: a timeout-less acquire froze a successor worker 15 min at 0 CPU).
- **`tests/ops/status/test_block_detach.py`** — a contract test for the bounded,
  holder-naming refusal.

### E. finding-29 — verify-relay one numeric grammar
- **`ops/decision/verify_relay.py`** — collapses the accreted per-format carve-outs
  (`float()` probes, `_is_fraction_digits`, `_is_integer_part_of_decimal`) into a
  single positive `_NUM_GRAMMAR` + `_is_number_literal()` consumed by both the
  source-collection and relay-audit sides; deletes the two decimal-span helpers.

### F. G2 — two new verbs, **registration only (dangling)**
- **`cli/_verb_module_map.py`**, **`operations.json`**, **`docs/generated/
  operations.md`**, **`docs/primitives/README.md`**, **`docs/internals/adding-a-
  primitive.md`** — register `host-retarget` (mutate) and `settle-run` (workflow),
  bumping the count 165 → 167.
- **The implementation modules were NOT in the stash.** No `ops/host_retarget.py`,
  `ops/settle_run.py`, or schema files appear as new-file hunks. The verb-map rows
  point at `hpc_agent.ops.host_retarget` / `...settle_run` modules that did not
  exist in the snapshot — i.e. the stash was a **broken mid-work state**, not a
  runnable checkpoint.

---

## 2. Relation to what landed

**Superseded draft.** Every workstream was committed in polished, expanded form
between `b3d0082c` and HEAD:

| Stash workstream | Landed in | Note |
|---|---|---|
| A. G9 dialect profiles | `e58b90d9` "per-family scheduler dialect profiles (G9) — #5 and #32 fixed, #6/#7/#63 consolidated" | `FamilyDialect`/`dialect_for`/`FAMILY_DIALECTS` present at HEAD (`profile.py:63/114/144`); landed version also consolidates #7/#63 |
| B. G4 engine keepalives | `5af09fe3` "G4 library-native lifecycle shrink (RULING 5, pulled forward pre-run-13)" | `_keepalive_interval` at `ssh_engine.py:323`; `_reap_if_idle` gone |
| C. G4 slot TTL removal | `5af09fe3` (same) | `SLOT_TTL_SEC` absent at HEAD; `_claim_is_stale` is pid-liveness-only |
| D. detached lease-lock | `5af09fe3` + follow-ons (`3cc794fa`) | `_LEASE_LOCK_TIMEOUT_SEC` at `detached.py:71`; HEAD goes **further** — adds stale-lease reclaim curing a permanent `DetachedLeaseHeld` wedge (`detached.py:326`) that the stash lacked |
| E. verify-relay grammar | `1602b7cd` "one positive numeric-literal grammar replaces the carve-out accretion (finding-29 residual)" | `_NUM_GRAMMAR`/`_is_number_literal` at HEAD; file later moved to `ops/decision/journal/verify_relay.py` by `f1bd4d47` "refactor(journal): split into package" |
| F. G2 host-retarget/settle-run | `d376b908` "G2 mechanization wave — host-retarget + settle-run (RULING 6; registry 167)" | Full modules **and** schemas **and** `_wire/workflows/` shims now exist; `settle_run` further hardened by `45d94feb` "receipt-gated terminal harvest" |

HEAD's operations count is **172** (142 primitive + 30 workflow) vs the stash's
target 167 — the tree has moved well beyond the snapshot. RULING 5 and RULING 6,
which this work implements, are both **already resolved** (the landing commits
cite them as done).

**Nothing in the stash is unique.** There is no orphan idea, no alternative
approach worth preserving — the landed commits are strict supersets (D and F
notably add hardening the draft never had).

---

## 3. Clean-apply status: **fully rotted, do not apply**

- `git apply --check` (plain) **fails on all 16 files** — every hunk's context has
  moved.
- `git apply --3way` merges the three regenerable files
  (`operations.md`, `operations.json`, `_verb_module_map.py`) to **OURS** via the
  `merge=generated` driver, but reports **conflicts** in six hand-written files:
  `_kernel/lifecycle/detached.py`, `infra/backends/_engine.py`,
  `infra/ssh_engine.py`, `infra/ssh_slots.py`,
  `tests/infra/backends/test_concurrency_cap.py`,
  `tests/infra/test_scheduler_states.py`, `tests/infra/test_ssh_engine.py`.
- The **verify-relay hunk cannot apply at all**: its target
  `src/hpc_agent/ops/decision/verify_relay.py` "does not exist in index" — the file
  was moved into the `journal/` package by `f1bd4d47`.

Reapplying would only reintroduce older versions of already-landed code (and the
dangling verb registrations from §1.F). There is nothing to salvage.

---

## 4. Decision

**Recommendation: DROP.**

- **DROP** — chosen. Rationale: 100% of the content landed in `e58b90d9`,
  `5af09fe3`, `1602b7cd`, `d376b908` (+ hardening in `3cc794fa`, `45d94feb`,
  `f1bd4d47`); the landed forms are supersets; the stash is a broken mid-work
  snapshot (dangling verb rows) that no longer applies. No open ruling depends on
  it (RULING 5 & 6 resolved).
- **CONVERT** — not warranted. There is no residual idea to spec into a build unit.
- **KEEP PARKED** — not warranted. Parking waits on a ruling; none is open, and the
  content is already in HEAD.

---

## 5. Commands

### To drop (recommended)
```sh
git -C "C:/Users/james/CC Allowed/hpc-agent" stash drop 'stash@{0}'
```
**Heads-up:** after this, **`stash@{1}` (the `creds` stash) becomes `stash@{0}`.**
That credentials stash is DO-NOT-DROP. Re-verify with `git stash list` immediately
after — you should see exactly one remaining entry, subject `On main: creds`.

### To sanity-check before dropping (optional, read-only)
```sh
# Confirm the WIP subject and that only these two stashes exist:
git -C "C:/Users/james/CC Allowed/hpc-agent" stash list
# Spot-check that the landed code is present (all should print matches):
git -C "C:/Users/james/CC Allowed/hpc-agent" grep -l "FAMILY_DIALECTS" -- src/hpc_agent/infra/backends/profile.py
git -C "C:/Users/james/CC Allowed/hpc-agent" grep -l "_NUM_GRAMMAR" -- src/hpc_agent/ops/decision/journal/verify_relay.py
```

### If you instead wanted to KEEP PARKED (not recommended)
No command — leave `stash@{0}` in place. But note it will never apply cleanly
again (§3), so parking it preserves nothing usable.

### CONVERT (not recommended)
N/A — no residual unit to build.
