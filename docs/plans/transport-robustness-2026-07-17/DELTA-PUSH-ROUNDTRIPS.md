# Delta-push round-trips — seam map + consolidation options (queued unit design memo)

2026-07-17 · **design-only** memo for the queued "delta-push round-trips" unit.
Target: the rsync-less DELTA path of `rsync_push`
(`src/hpc_agent/infra/transport/__init__.py:862–990`), which
[`AUDIT.md`](AUDIT.md) §2 rates *"the highest round-trip count in the stack — a
prime `sync-files` consolidation target"* and §4 ranks **Rank 4** (highest
round-trip count anywhere AND the live native-Windows push path). This page is
the durable seam map → options → recommendation → drift log for that unit. No
source touched.

**Concurrency note:** a separate agent is editing the **stage-swap** region of
`transport/__init__.py` (~lines 340–430, the `delete=True` full-copy path — unit
**U4**). This memo is built from the DELTA-path region (862–990) and READ-only
against the stage-swap region. The one overlap — the primary's per-batch fold
touches `_tar_ssh_push` (def `:421`), the same *function* U4 edits but its
**additive `delete=False` / `only_paths` remote-command branch**, distinct from
U4's `delete=True` stage-swap branch — is flagged explicitly in §4 and §6.

The delta path is gated to `delete=True` user-tree pushes on rsync-less hosts
(`:871` `delta_on = delete and env != _DELTA_ENV_KILL`); it is the LIVE staging
path on native Windows (the demo/relay box). Everything below is the
`sync-files` primitive's real cost on that platform.

---

## 1. Today's exact legs — a warm re-push of a typical small delta

Every leg opens a **fresh cold connection** (no ControlMaster on native Windows),
so leg-count == cold-dial-count == intrusion-filter/ban-risk count == drop-point
count. `throttle_connection(ssh_target)` (`:844`) paces the opens (no-op unless
`HPC_SSH_SAFE_INTERVAL>0`) but does not reduce them.

Formula for a delta of **N batches** (`N = ceil(delta / _delta_batch_caps()`,
2000 files / 256 MiB, `_delta.py:197`):

> **legs = 1 (manifest read) + N (tar pushes) + (N−1) (per-batch checkpoints) + P (prune: 0/1/2) + 1 (final seal)  =  2N + 1 + P**

| # | Leg | Call site | Runs through | Breaker/slot-gated? | Fires when |
|---|---|---|---|---|---|
| A | remote hash-manifest read | `_remote_push_manifest` `:872–878` → `_delta.py:446` → `_ssh_bounded` | `_ssh_bounded` `:397` (direct `run_capture_bounded`) | **NO** — un-guarded | always (delta_on) |
| B×N | tar\|ssh batch push (`delete=False`, `only_paths=batch`) | `guarded_call(_tar_ssh_push)` `:929–942` | `guarded_call` `ssh_circuit.py:663` | **YES** | per batch (skipped when `ship` empty, `:962`) |
| C×(N−1) | per-batch push-manifest checkpoint | `_write_push_manifest` `:955–961` → `_prune.py:137` | `_ssh_bounded` (direct) | **NO** — un-guarded | `i < len(batches)` only (never for N=1) |
| D1 | prune plan — read prior manifest | `_read_prior_push_manifest` `_prune.py:63` (from `_prune_manifest_known_extras` `:741`) | `_ssh_bounded` (direct) | **NO** — un-guarded | only if `delta.extra` has non-bookkeeping candidates (`:733`) |
| D2 | prune `rm` | `_execute_prune` `_prune.py:203` (`:783`) | `_ssh_bounded` (direct) | **NO** — un-guarded | only if a prunable plan survives the cap |
| E | final manifest seal (paths ∪ retained-extras) | `_write_push_manifest` `:984–989` | `_ssh_bounded` (direct) | **NO** — un-guarded | always (delta path) |

**Concrete counts:**

| Scenario | Legs | Which |
|---|---|---|
| Small delta, a few files changed, **nothing dropped** (N=1, P=0) | **3** | A, B, E |
| Small delta, a file dropped **and pruned** (N=1, P=2) | **5** | A, B, D1, D2, E |
| Nothing changed at all (N=0, `ship` empty, P=0) | **2** | A, E (no tar) |
| Large re-ship, N batches, nothing dropped | **2N+1** | A, B×N, C×(N−1), E |

**First deploy** (no remote manifest — `_remote_push_manifest` returns `None`:
`cd` fails / absent tree / pre-delta runtime): the delta path is NOT taken. Legs:

| # | Leg | Site |
|---|---|---|
| 1 | manifest probe (returns `None`) | `_remote_push_manifest` `:872–878` — **one wasted round-trip** |
| 2–5 | full-copy staged tar (stage-drop · extract · preclean · swap) | `guarded_call(_tar_ssh_push delete=True)` `:1005–1016` → the 4-leg stage-swap ([`STAGE-SWAP-SEAM-MAP.md`](STAGE-SWAP-SEAM-MAP.md), U4 territory) |

≈ **5 legs**; the full-copy fallback writes **no** push manifest (`:1005` returns
directly), so the first *delta* re-push re-derives `paths` from `∅`. First-deploy
consolidation is U1/U4 territory (the stage-swap) and out of scope here except
for the wasted probe leg (noted §4, option 6).

### Latency-serial vs pipelined

**Every leg A–E is latency-serial** — each is a separate `_ssh_bounded` /
`guarded_call` that completes (client reads its result) before the next starts,
with a client-side gap and a fresh handshake between. There is **no pipelining
across legs today.** The *only* concurrency is **inside** a single B leg: the
`tar c | ssh tar x` pump runs both Popen halves at once over one pipe
(`_tar_ssh_push` `:451`). So the round-trip count is a pure serial-latency
multiplier, and each extra leg is an independent cold-dial ban-risk count.

**Finding (breaker coverage):** of the small-delta legs, **only the tar pushes
(B) are breaker/slot-gated** (`guarded_call`). The manifest read, every
checkpoint, both prune legs, and the final seal are un-guarded direct
`_ssh_bounded` dials (AUDIT §6 records this class). So 4 of the ~5 legs of a
pruning small delta bypass the per-host breaker and the N=2 slot cap — folding
them *into* the guarded B leg is therefore also a **U5 (breaker/slot uniformity)
win**, not just a count reduction.

---

## 2. The load-bearing invariants any option must preserve

The delta path's resumability is a designed mechanism (run-13 finding 3,
`:901–961`). Any consolidation is **disqualified** if it breaks one of these:

1. **A mid-op drop must resume, never re-transfer landed data.** Each landed
   batch is durable; a retry's delta is re-derived from the *live remote hash*
   (leg A) and re-ships only the remainder. This holds because the remote tree
   itself is the source of truth — NOT the `.push_manifest.json`. The manifest's
   `paths` list is **prune bookkeeping only**; a stale/missing `paths` never
   causes a re-transfer (the hash manifest still re-derives the delta).
2. **Prune stays fail-open.** A path is deleted only if we can PROVE it is ours
   (recorded in the prior push manifest, `_prune.py` docstring). Any failure —
   unreadable manifest, severed leg, garbled field — must route every remote
   extra to the **ANOMALY** branch (never deleted), never to fail-closed.
3. **Remote writes stay atomic** (temp + `os.replace`, `_PUSH_MANIFEST_MERGE_PY`
   `_prune.py:119`), so a torn checkpoint never corrupts the live manifest.
4. **House disciplines:** positive-evidence ack on any *new* remote read;
   stdlib-floor `python3`, no activation (`_delta.py:44`); bounded timeouts; no
   new raw ssh (`lint_no_raw_ssh` — reuse `_ssh_bounded`/`guarded_call`).

The safe direction throughout: the *remote tree* re-derives the delta, so a
dropped checkpoint/seal is only ever a **prune-bookkeeping lag** (extras degrade
to un-prunable anomalies = fail-open), never a correctness loss and never a
re-transfer.

---

## 3. Consolidation options (each with its drop-mid-leg verdict)

### Option 1 — fold the prune-plan read into the manifest read (one script returns both)

`_REMOTE_MANIFEST_SNIPPET` (`_delta.py:70`) **already opens
`.hpc/.push_manifest.json`** to read the `entries` quick-check cache (`:99–109`).
The prune's separate `cat` leg (**D1**, `_read_prior_push_manifest`) reads the
same file for its `paths` list. Emit `paths` alongside `files` in the snippet's
JSON; the control plane then has `known` from leg A and drops D1 entirely.

- **Δ legs:** −1 whenever a prune fires.
- **Drop mid-leg:** it is a pure READ fold. A drop still yields no stdout →
  `_parse_remote_push_manifest` → `None` → full-copy fallback, unchanged (F1
  invariant untouched — the fold cannot make a drop re-transfer).
- **Prune fail-open?** YES-preserved. Absent/garbled `paths` → `known = ∅` →
  every extra → ANOMALY (never deleted). Parse must tolerate a missing field
  (v1 manifests, first deploy) exactly as `_read_prior_push_manifest` does today.
- **House disciplines:** stdlib-floor snippet already emits JSON; adding a field
  is additive; positive evidence is the existing "JSON-or-None → fallback".
- **Verdict: QUALIFIES.** Lowest-risk win in the set; touches only `_delta.py`
  + the prune caller. No `_tar_ssh_push` edit.

### Option 2 — ride the per-batch checkpoint inside its tar-push leg (remote-side append, ack-gated)

Today each batch is `guarded_call(_tar_ssh_push)` (B) **then** a separate
`_write_push_manifest` (C). The checkpoint payload — `base_paths ∪ landed`
(`:917`, `:959`) — is fully known **before** the push. Bake it into the SAME ssh
invocation: after `tar x` lands, run the existing `_PUSH_MANIFEST_MERGE_PY` and
emit a positive-evidence sentinel:

```
tar x -C <r> ; ( printf %s <payload_b64> | base64 -d \
    | HPC_PM_PAYLOAD=<b64> python3 && printf '__HPC_PUSH_CP_OK__' ) || true
```

- **Δ legs:** −(N−1) (every intermediate checkpoint disappears into its push).
- **Drop mid-leg — the load-bearing case:**
  - Sever DURING `tar x`: extract partial, merge never runs, no `__CP_OK__`.
    Next push re-derives from remote hash → ships remainder. **Resumes, no
    re-transfer of prior batches.**
  - Sever AFTER `tar x` but BEFORE `__CP_OK__`: the batch DID land, but the
    client sees rc≠0 / no ack and treats the checkpoint as **not done**. Next
    push's leg A re-derives the now-present files → ships only remainder; the
    lagged `paths` only defers prune (fail-open). **Correct + resumable.**
  - The `; … || true` (not `&&` off tar x) keeps tar x's rc authoritative so a
    merge hiccup can never fail an otherwise-good batch (merge is best-effort,
    matching today's fail-open `_write_push_manifest`).
- **Ack contract (NEW remote read-back):** the client counts the checkpoint as
  committed **only** on seeing `__HPC_PUSH_CP_OK__` in the leg's stdout —
  positive-evidence per discipline (4). Absence ⇒ un-checkpointed ⇒ safe
  re-derive. This is the one genuinely new ack the unit introduces.
- **Prune fail-open?** Unaffected — checkpoints write `paths`; a missed one only
  lags prune.
- **Cost:** touches `_tar_ssh_push`'s `delete=False` remote-command construction
  (new optional `checkpoint_payload_b64` param) — the **U4-adjacent** edit.
  This is the "remote-side complexity" the fallback (§5) avoids.
- **Verdict: QUALIFIES** (resumable + fail-open preserved) **with the mandatory
  `__HPC_PUSH_CP_OK__` ack.** Also a U5 win: the checkpoint moves from an
  un-guarded direct dial into the `guarded_call`-wrapped B leg (under the breaker
  + slot).

### Option 3 — fold the final manifest seal into the last batch's leg

Same mechanism as Option 2 applied to the last batch: its remote append writes
the **final** manifest (`local entries`), removing leg **E** for the no-prune
case. The wrinkle: E today writes `local entries ∪ retained_extras` (`:987`), and
`retained_extras` is only known AFTER the prune runs (which follows the last
batch). Resolution: the last batch's append writes the **provisional** manifest
(local entries only); a prune tail (§ Option 4) re-seals with the union **only
when extras exist**. When nothing is dropped (the common warm case), no tail
fires and E is fully absorbed.

- **Δ legs:** −1 in the no-prune case (the overwhelming warm-re-push case).
- **Drop mid-leg:** identical to Option 2 (ack-gated; drop ⇒ re-derive). A
  provisional seal that never acks just lags prune bookkeeping (fail-open).
- **Prune fail-open?** Preserved — the union re-seal (when it fires) is the
  existing `:984` write.
- **Verdict: QUALIFIES** (bundled with Option 2; same ack, same
  `_tar_ssh_push` touch).

### Option 4 — collapse the prune `rm` + retained-union seal into one trailing script

When a prune DOES fire, fold **D2** (`rm`) and the union re-seal (E) into a
single trailing `_ssh_bounded` leg: one stdlib-floor script that
`rm -f -- <prunable>` then writes the manifest as `local entries ∪ (prunable it
failed to delete)`, atomically. `known`/plan come from Option 1's folded read, so
D1 is already gone.

- **Δ legs:** the pruning tail goes 2→1 (D2 + E → one leg); with Option 1 the
  whole prune path D1+D2+E collapses toward a single tail.
- **Drop mid-leg:** script severs after `rm` before write → the extras stay
  deleted (correct — they were proven-ours) and the manifest lag re-derives next
  push (F1). If the script severs before `rm`, nothing deleted, retained set
  intact next push. **Fail-open preserved** — a severed prune is a skipped prune.
- **Correctness cost:** the retained-set union must be computed **remote-side**
  (which paths the `rm` failed to remove) to fold cleanly, OR keep it client-side
  by having the script emit which rm's failed (ack-listed) and the client
  re-seals — the latter re-introduces a leg, so prefer the remote-side union.
  Moderate remote-script complexity; still stdlib-floor.
- **Verdict: QUALIFIES.** Best kept as the FALLBACK's core (it needs no
  `_tar_ssh_push` edit).

### Option 5 — enlarge batch caps to cut N (rejected as a primary)

Raising `_delta_batch_caps()` (`_delta.py:222`) cuts N, hence C and B legs. But
larger batches make a died batch **re-pay more** in-flight bytes on retry — it
trades dial-count for resumability granularity, the exact tradeoff run-13
finding 3 tuned. **Disqualified as a standalone win** (weakens resumability, F1).
Note the *inverse* synergy with Options 2/3: once checkpoints ride the tar leg
for free, batch size is **decoupled from dial count**, so you may keep batches
SMALL (tighter resumability) at no per-checkpoint dial cost — a positive side
effect, not a lever to pull alone.

### Option 6 — skip the wasted first-deploy manifest probe (minor)

On first deploy, leg A fires and returns `None` before the full-copy fallback.
A cheap `test -d .hpc` could be folded into the fallback rather than paying a
full snippet probe — but the probe IS the delta/no-delta decision and returning
`None` is already the cheap path. **Low value; note only.** First-deploy leg
reduction belongs to U1/U4 (the stage-swap), not this unit.

### Connect-throttle / breaker interaction (applies to all options)

Fewer legs ⇒ fewer `throttle_connection` opens and fewer cold handshakes ⇒
lower ban-risk on a rate-limited login node (the run-11 CARC class). Folding the
un-guarded checkpoint/seal/prune dials into the guarded B leg additionally brings
them under the breaker + N=2 slot (a U5 alignment). **No option adds slot
pressure** — they all *remove* dials; there is no new-deadlock surface because a
folded write rides a dial the push already holds (the same zero-new-cold-SSH
discipline `_prune.py` already follows for the prune).

---

## 4. PRIMARY recommendation — "read-once, ack-append, seal-once"

Combine **Options 1 + 2 + 3** (with Option 4 as the pruning tail):

1. **Fold D1 into A** (Option 1): `_REMOTE_MANIFEST_SNIPPET` emits the prior
   `paths`; the delta path passes the folded `known` into
   `_prune_manifest_known_extras`, dropping `_read_prior_push_manifest`.
2. **Ride each checkpoint + the final seal inside its tar leg** (Options 2+3),
   ack-gated by `__HPC_PUSH_CP_OK__`. No separate C legs; no separate E in the
   no-prune case.
3. **Pruning tail** (Option 4): when — and only when — `delta.extra` yields
   prunable candidates, one trailing `_ssh_bounded` leg does `rm` + union-reseal.

### Round-trip reduction (before → after)

| Scenario | Before | After | Δ |
|---|---|---|---|
| Small warm delta, nothing dropped (N=1, P=0) | **3** (A,B,E) | **2** (A, B+seal) | −1 |
| Small warm delta, a file pruned (N=1, P=2) | **5** | **3** (A, B+provisional-seal, prune-tail) | −2 |
| Large re-ship, N batches, nothing dropped | **2N+1** | **N+1** (A + N B's, each carrying its own checkpoint/seal) | −N |
| Large re-ship, N batches, with prune | **2N+3** | **N+2** (A + N B's + prune-tail) | −(N+1) |

Headline: the live native-Windows warm re-push drops **3→2** (no prune) or
**5→3** (with prune); a large 10-batch re-ship drops **21→11**. And ~4 formerly
un-guarded dials per push move under the breaker/slot.

> **Count correction (verified at build, 2026-07-17).** The two large-re-ship
> `After` cells above were originally written **N** / **N+1** (headline "21→10"),
> undercounting by one — they omitted the constant leg **A** (the remote hash
> read), which is present on every delta push. The small-delta `After` rows,
> written explicitly as "A, B+seal" = 2, always carried it and are correct; the
> general formula is **A(1) + N tar legs (self-sealing) [+ 1 prune-tail]**, i.e.
> **N+1** no-prune / **N+2** with-prune. The pins encode the verified counts (see
> the drift log's Option-3 entry).

### Files / functions touched

| File | Change |
|---|---|
| `transport/_delta.py` | `_REMOTE_MANIFEST_SNIPPET` (`:70`) emit `paths`; `_remote_push_manifest` (`:446`) / `_parse_remote_push_manifest` (`:421`) return `(manifest, known_paths)` |
| `transport/__init__.py` delta path (`:862–990`) | consume folded `known`; pass `checkpoint_payload_b64` into per-batch `_tar_ssh_push`; drop per-batch `_write_push_manifest` (`:955`) + final `_write_push_manifest` (`:984`) in the folded cases; gate on `__HPC_PUSH_CP_OK__` |
| `transport/__init__.py` `_tar_ssh_push` (`:421`) | **`delete=False`/`only_paths` branch only** — optional `checkpoint_payload_b64` param appending the ack-gated merge after `tar x`. **U4-ADJACENT: coordinate/rebase — U4 edits the `delete=True` stage-swap branch of the same function** |
| `transport/_prune.py` | `_write_push_manifest` (`:137`) grows the `__HPC_PUSH_CP_OK__` sentinel; `_read_prior_push_manifest` (`:63`) retired from the delta path (retain for back-compat/other callers); prune tail = `rm`+union-reseal script (extends `_execute_prune` `:203` + `_PUSH_MANIFEST_MERGE_PY` `:119`) |

### Pins to extend

- `tests/infra/test_transport_delta_cache_checkpoint.py` — the checkpoint-cadence
  + schema-lockstep pin: assert the checkpoint now **rides the tar leg** and is
  ack-gated; assert byte-identical `paths`/`entries` result vs the old separate
  write.
- The `_delta_ship_batches` determinism pin (`_delta.py:232`) — unchanged, but
  re-assert the folded payload is computed in the same input order.

### New fault-injection drills owed (extend [`FAULT-HARNESS.md`](FAULT-HARNESS.md), currently 18 drills)

This unit provides the product seam FAULT-HARNESS §4 lists as **needed** for
AUDIT §7 row 8 ("kill ssh mid-`tar|ssh` push", owner U1) — move it to §2 covered:

1. **Sever after `tar x`, before `__HPC_PUSH_CP_OK__`** → assert the batch data
   is durable (next delta re-derives, no re-transfer) AND the client treats the
   checkpoint as NOT committed (never trusts an un-acked checkpoint). *The core
   Option-2 ack drill.*
2. **Garble the folded `paths` field** (present, wrong shape) → assert prune
   degrades to `known=∅` (every extra → ANOMALY, never deleted) — fail-open
   preserved (Invariant 2).
3. **Sever mid folded manifest read** → assert `None` → full-copy fallback
   (the added field must not change the None-on-trouble contract — Invariant 1).
4. **Sever the pruning tail after `rm` before reseal** → assert deleted extras
   stay deleted + manifest lag re-derives next push (Invariant 2).
5. Extend the existing push-pump drill (§4 row 8) so a mid-batch sever leaves
   **prior** landed batches durable *with their now-folded, acked* checkpoints.

Use the existing `sever_at` / `hang_at` / `garble_at` / `fake_clock` vocabulary;
assert doctrine outcomes (durable / re-derive / ANOMALY / no re-transfer) only.

---

## 5. FALLBACK — if the remote-side append complexity is unwanted

If the `_tar_ssh_push` remote-command edit is undesirable (e.g. to avoid all U4
coupling, or to keep `_tar_ssh_push` a pure byte-pump with no manifest logic),
take **Options 1 + 4 only** — the pure read/tail folds that never touch
`_tar_ssh_push`:

- **Option 1:** fold D1 (prune read) into leg A. −1 leg when pruning.
- **Option 4:** fold D2 (`rm`) + E (final seal) into one trailing script leg.

| Scenario | Before | Fallback after |
|---|---|---|
| Small warm delta, nothing dropped | 3 | **3** (unchanged — A,B,E; E can't fold without the tar-leg append) |
| Small warm delta, a file pruned | 5 | **3** (A, B, combined `rm`+seal tail; D1 folded into A) |
| Large re-ship, N batches | 2N+1 | **2N** (only the tail folds; per-batch C legs stay separate) |

Smaller win, **zero `_tar_ssh_push` edits, zero U4 collision**, minimal
remote-script surface (reuses `_PUSH_MANIFEST_MERGE_PY` + `_execute_prune`
shapes). All four load-bearing invariants hold trivially (no new ack needed —
the tail write is fail-open exactly as today). The pruning small delta still
drops 5→3; only the large-N per-batch checkpoint saving is forgone.

---

## 6. Open coordination + drift log

- **U4 overlap — RESOLVED (2026-07-17).** Option 2 landed AFTER U4 (378b3f53).
  The Option-2 append lives entirely inside `_tar_ssh_push`'s `delete=False`
  extract branch (gated `if checkpoint_payload_b64 is not None`, which only the
  delta batch path passes with `only_paths`), disjoint from U4's `delete=True`
  stage-swap tail — the two branches never touch. No rebase collision occurred.
- **U1 seam handoff:** the primary supplies the push-pump injection seam
  FAULT-HARNESS §4 row 8 marks "needed" — land the seam + its drills in the same
  PR (FAULT-HARNESS §5.2).
- **U5 alignment:** folding the un-guarded checkpoint/seal/prune dials into the
  guarded B leg advances U5 (breaker/slot uniformity) for free; note it in the
  U5 unit so the contract test's exemption list stays accurate.

### Drift log

- 2026-07-17: created, design-only. No code changed by this unit. Built from
  `transport/__init__.py:796–990` + `_delta.py` + `_prune.py` +
  `ssh_circuit.guarded_call:663` at c893d2fa; stage-swap region (~340–430) read
  READ-only while a concurrent agent (U4) edited it.
- 2026-07-17: **BUILT — the §5 FALLBACK (Options 1 + 4); Options 2 & 3 DEFERRED.**
  Landed the two folds that never touch `_tar_ssh_push` (U4's surface), per the
  restricted build scope:
  - **Option 1 (fold D1 into A).** `_REMOTE_MANIFEST_SNIPPET` now emits the prior
    manifest's `paths` bookkeeping in its own JSON; `_parse_remote_push_manifest`
    / `_remote_push_manifest` return `(manifest, known)`; the delta path threads
    `remote_known` into the prune. The standalone `_read_prior_push_manifest`
    dial is gone from the delta hot path (retained for back-compat, docstring
    notes it). Fail-open preserved: absent/garbled `paths` → `known = ∅` → every
    extra → ANOMALY; the manifest read keeps its None-on-any-trouble contract
    (the added field never changes it).
  - **Option 4 (collapse D2 `rm` + E union-reseal into ONE only-when-extras
    tail).** New `_prune_and_reseal` + `_PRUNE_RESEAL_PY` (stdlib-floor,
    temp+`os.replace` atomic, `entries` cache preserved, retained survivors
    computed REMOTE-SIDE). `_prune_manifest_known_extras` now OWNS the single
    trailing leg: the combined tail when a prune fires, else a standalone
    `_write_push_manifest` seal. Exactly one trailing leg either way.
  - **Round-trips (proven by pins):** small warm delta nothing-dropped stays
    **3** (A,B,E); small warm delta with a prune drops **5 → 3** (A folds D1; B;
    the combined tail folds D2+E). Leg-count pins: `test_warm_delta_push_no_prune_
    is_three_legs`, `test_warm_delta_push_with_prune_is_three_legs`. Invariants
    held: mid-drop resume re-derives from the live remote hash (batch/checkpoint
    mechanics untouched — `test_died_mid_push_retry_ships_only_the_remainder`);
    prune fail-open (`test_prune_reseal_script_deletes_seals_and_retains_survivors`
    — a raced/failed delete stays ours).
  - **DEFERRED — Options 2 & 3 (the memo's §4 PRIMARY extras).** The per-batch
    checkpoint fold (Option 2) and the final-seal fold into the last batch's leg
    (Option 3, memo numbering) both edit `_tar_ssh_push`'s `delete=False` branch —
    U4's function. Land after the U4 merge (§6 coordination note). This unit's
    diff is confined to `_delta.py` / `_prune.py` / the delta-path caller +
    the re-export block, so it merges cleanly around U4's stage-swap edits.
- 2026-07-17: **BUILT — Option 2 (the per-batch checkpoint fold), the deferred
  half now UNBLOCKED (U4 landed at 378b3f53).** Each mid-ship push-manifest
  checkpoint now RIDES its tar-push leg instead of a separate `_write_push_manifest`
  dial:
  - **Sentinel protocol.** New `_PUSH_CP_SENTINEL = "__HPC_PUSH_CP_OK__"` +
    `_folded_checkpoint_cmd(remote_path, payload_b64)` (both `_prune.py`) build the
    ack-gated append `_tar_ssh_push` bolts onto its `delete=False` extract:
    `mkdir -p <r> && tar x -C <r> && { ( cd <r> && mkdir -p .hpc && <merge> &&
    printf %s __HPC_PUSH_CP_OK__ ) || true; }`. The append is **`&&`-gated on
    `tar x`** (rc stays authoritative — a failed extract short-circuits it, so a
    batch death still returns rc≠0 and the caller's early-return/resume path is
    unchanged) and **`{ ( … ) || true; }`-wrapped** (a best-effort merge hiccup can
    never fail an otherwise-good batch). The sentinel prints LAST and only after
    the merge `python3` succeeds; its PRESENCE in the leg's stdout is positive
    evidence the checkpoint committed, its ABSENCE a safe re-derive (never read as
    a batch failure). A shared `_push_manifest_payload_b64` makes a folded and a
    standalone checkpoint byte-identical; the merge reuses `_PUSH_MANIFEST_MERGE_PY`
    (same crash-safe temp+`os.replace`, `entries`-preserving). Rides the same
    guarded/breaker dial the batch already holds (U5) — no raw ssh, no new cold SSH.
    A `_disclose_checkpoint_uncommitted` line keeps the log honest when a mid-ship
    ack is absent.
  - **Round-trips (before → after, proven by pins).** Small warm delta (N=1) is
    UNCHANGED — 3 (no-prune) / 3 (prune): N=1 has no mid-ship checkpoint, so the
    §5 pins stay green. **Large re-ship, N batches, nothing dropped: 2N+1 → N+2**
    (the N−1 mid-ship checkpoint dials disappear into their tar legs; only leg A +
    N batches + the final standalone seal E remain). For N=3, **7 → 5**. New pin
    `test_large_delta_push_folds_checkpoints_into_batch_legs` (first N−1 tar legs
    carry the ack-gated fold, last does not, exactly one standalone seal survives);
    `test_large_delta_push_batch_legs_are_preamble_free` extends E1 to the folded
    leg. Option 3 (fold E into the last batch → N) remains the only deferred item.
  - **Invariants held.** A mid-drop RESUMES without re-transferring landed batches
    — `tar x` rc governs batch-landed, the sentinel governs checkpoint-committed
    (orthogonal); the retry re-derives from the live remote hash and ships only the
    remainder (faultinject Drill 2 extended to assert the fold rides the leg + the
    last batch carries none; new Drill 2b `test_sentinel_absence_after_landed_batch_
    is_not_a_failure` pins the drop-after-`tar x`-before-ack case). Prune stays
    FAIL-OPEN (a missed checkpoint only lags bookkeeping; a later batch's cumulative
    checkpoint subsumes it, and the final seal covers all). Files touched:
    `_prune.py` (sentinel + payload/cmd builders + `_write_push_manifest` refactor),
    `_disclose.py` (uncommitted-ack line), `transport/__init__.py` (`_tar_ssh_push`
    param + delta-loop fold + re-exports). Verified: targeted transport + delta +
    faultinject + full `tests/contracts/` green; lint gauntlet (incl. `lint_no_raw_ssh`
    + `lint_remote_read_ack`) + regen `--check` + ruff/format/mypy clean.
- 2026-07-17: **BUILT — Option 3 (the final-seal fold), the last deferred item —
  the delta-push round-trip consolidation is now COMPLETE (Options 1 + 2 + 3 + 4).**
  The LAST delta batch now carries the FINAL provisional seal on its own tar-push
  leg, so the separate final-seal dial (leg E) is absorbed for the no-prune warm
  re-push:
  - **The fold is free of a new mechanism.** After the last batch, the Option-2
    cumulative checkpoint payload — `base_paths ∪ landed ∪ batch` — EQUALS the full
    local entry set = the final provisional seal (`sorted(seal_paths)`). So Option 3
    is exactly "extend the Option-2 fold to the LAST batch too": the delta loop drops
    its `if i < len(batches)` guard and EVERY batch rides its cumulative write
    (`_push_manifest_payload_b64`), ack-gated by the SAME `__HPC_PUSH_CP_OK__`
    sentinel via `_folded_checkpoint_cmd`. No new remote script, no new sentinel.
  - **`seal_folded` skip.** `rsync_push` passes `seal_folded=True` (whenever ≥1 batch
    shipped) into `_prune_manifest_known_extras`; its `_seal` helper SKIPS the
    standalone `_write_push_manifest` when the retained set is empty (leg E fully
    absorbed — the common warm case), but STILL writes when a cap-refused/all-anomaly
    RETAINED set adds paths the provisional did not carry (#F58 provenance intact).
    The Option-4 `_prune_and_reseal` tail fires unchanged when a prune has paths —
    it OVERWRITES the provisional with the authoritative retained-union — so the
    pruning case is untouched.
  - **Orthogonality (load-bearing).** `tar x` rc governs batch-LANDED; the sentinel
    governs SEALED. A last-batch drop after `tar x` before the seal ack leaves the
    tree durable and only the reseal lagged; the next push re-derives the (empty)
    delta from the live remote hash, ships NOTHING (no re-transfer), and re-seals via
    the standalone leg (`seal_folded=False` on an empty ship). NO corrective dial —
    that would re-introduce leg E. A new `_disclose_seal_uncommitted` line keeps the
    log honest on an absent final-seal ack.
  - **Round-trips (verified by pins).** No-prune warm re-push: N=1 **3 → 2**
    (`test_warm_delta_push_no_prune_is_two_legs`), large N **N+2 → N+1**
    (N=3 **5 → 4**, `test_large_delta_push_folds_every_checkpoint_and_the_final_seal`).
    WITH-prune is UNCHANGED (the Option-4 tail is still its own leg): small stays 3
    (`test_warm_delta_push_with_prune_is_three_legs`), large stays N+2. This is the
    §4 table's `−1 on the no-prune cases`, with the table's off-by-one corrected
    above (its large `After` omitted leg A). `test_every_batch_folds_its_write_and_
    the_last_is_the_final_seal` pins that every batch (incl. the last) folds and no
    standalone seal survives; `test_seal_folded_absorbs_the_standalone_seal_only_
    when_no_retained` + `test_seal_folded_still_fires_the_prune_reseal_tail` pin the
    skip logic; faultinject Drill 2 updated (last batch now folds), Drill 2b extended
    (final-seal ack absence), new **Drill 6**
    `test_last_batch_seal_ack_absent_durable_then_reseals_without_retransfer` pins the
    resume-after-drop-before-seal-ack → durable → re-seal, no re-transfer.
  - **Numbering reconciliation.** The source (`_prune.py`, `transport/__init__.py`)
    and two tests (`test_transport_prune.py`, `test_remote_rsync_fallback.py`) had
    MISLABELED the prune-`rm`+reseal collapse as "Option 3"; per this memo's §3/§4 it
    is **Option 4** (Option 3 is the final-seal fold). All such labels are corrected
    to Option 4, and the final-seal fold now owns "Option 3" — the code, the
    faultinject drills, and this memo now agree. Files touched:
    `transport/__init__.py` (drop the last-batch guard + `seal_folded` param/skip +
    `_disclose_seal_uncommitted` re-export + relabel), `_disclose.py`
    (`_disclose_seal_uncommitted`), `_prune.py` (relabel Option 3→4), plus the four
    test files. Verified: targeted transport/prune/delta/rsync-fallback + faultinject
    + full `tests/contracts/` green; `run_lint_gauntlet` (incl. `lint_no_raw_ssh` +
    `lint_remote_read_ack`) + regen `--check` + ruff/format/mypy clean.
