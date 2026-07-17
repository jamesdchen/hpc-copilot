# Amortized reduction check — execute the proposed reducer on real evidence during dead time

2026-07-17 · baseline `main @ 6d29e23b` (clean tree). **Design-only unit** — the
sole file this unit writes is this memo. No `src/**` change, no commit. Two other
sessions are concurrently editing `ops/submit_blocks.py` (the S1 static
reducibility disclosure) and `state/*` (U3 phase-1); this memo READS only and
flags the coordination points.

## The directive

> "ensure that any reduction that the LLM proposes is checked to make sure it
> works, in an amortized fashion — checked when we expect to be waiting, perhaps
> while we wait for the canary to run or during s3 or something, not sure when is
> best."

The static S1 reducibility DISCLOSURE (concurrent unit, `docs/plans/s4-gaps-2026-07-17.md`
item 1, ruled today) is **necessary but not sufficient**. It answers "is there a
declared path to a number?" from the sidecar — a *predicate*, never an execution.
A declared custom `aggregate_cmd` makes that predicate PASS while saying nothing
about whether the reducer actually runs: a py3.13-syntax reducer against a login
py3.8, a missing `import pandas` in the run env, a wrong output path, a reducer
that emits non-JSON — every one of these passes S1 and dies at final harvest,
after the whole array has computed. The directive wants the proposed reduction
**executed against real evidence during dead time**, so a broken reducer is
discovered *before the array finishes*, at zero added wall-clock.

This memo maps the wait windows, designs the check per viable window, situates it
as the middle rung of a three-rung ladder, and recommends what to build first.

---

## 1. The wait windows (ground truth from code)

The submit lifecycle is a code-driven block chain: **S1** (`submit-s1`, resolve +
mint sidecar, `submit_blocks.py:~600-758`) → **S2** (`submit-s2`, stage & canary,
STOP, `submit_blocks.py:~797-960`) → human greenlight → **S3** (`submit-s3`,
`launch_main_array`) → detached watch → reconcile ticks → aggregate. The genuine
dead windows and the evidence live at each:

| # | Window | Rough duration | Evidence available at that moment |
|---|--------|----------------|-----------------------------------|
| (a) | **Canary queue + run** (`verify_canary`, `wait_budget_sec` default **1800s**; a fast canary lands seconds-to-minutes via `HPC_CANARY_FAST_POLL_SEC` ramp) | queue-wait + one real task's runtime | **NONE until the window ENDS**, then: the canary's task-0 result dir — a **genuine task artifact** (`metrics.json` / the declared summary artifact) for ONE real row. The natural fixture. |
| (b) | **S2 → S3 human boundary** (human reviews "canary green, est N core-hours", greenlights `submit-s3`) | unbounded human think-time; seconds-to-minutes typical | the canary output from (a) already exists on the cluster **and** is already pulled locally for the fingerprint (`_pull_canary_task0_metrics`). Pure control-plane dead time — nothing on the critical path. |
| (c) | **S3 staging/deploy** (`launch_main_array`) | short (qsub + detach) | **NO NEW EVIDENCE**: rsync + deploy already ran in the canary's Phase-1 prelude (`submit_and_verify`, before the canary). The reducer is already shipped (`d2b18bfd` derive+ship). S3 stages nothing the canary window didn't already have. |
| (d) | **First-wave partial harvest** (reconcile tick after wave 0 completes) | after wave-0 compute is spent | wave-0's per-task outputs — a **fuller fixture**: multiple rows, one grid point, possibly ≥2 arms. Exercises what one canary row cannot. But this is *after* main compute has begun — a later, more expensive rung. |

**The honest reading of "amortize during the wait."** The canary RUN (window a) is
itself the wait; the reducer's fixture does not exist until that window ends. So
the check *executes* at canary-terminal and its compute overlaps window (b) — the
human reading the S2 brief. The array does not launch until the S3 greenlight
regardless, so a check that finishes during (b) adds **zero wall-clock to the
critical path**. That is the amortization: dead-time (b), fixture from (a),
verdict carried onto the S2/S3 brief.

Window (c) is a decoy — no fresh evidence. Window (d) is a real but *later* rung.

---

## 2. The check, per viable window

### 2.1 The canary window (a)+(b) — evaluate hardest, the primary design

When the first canary verifies `ok` and is marked terminal
(`submit_and_verify.py:1046`, `_mark_canary_terminal(..., status="complete")`),
its task-0 output **is a genuine task artifact** — the exact same shape the main
array's tasks will write. Run the run's proposed reducer over that one row.

**Mechanism — reuse `cluster_reduce` verbatim (one-definition).** The check does
not invent a reduction path; it calls the SAME primitive the final harvest calls:

```
cluster_reduce(
    experiment_dir,
    run_id       = f"{main_run_id}-canary",   # the canary is a real journaled run
    aggregate_cmd = <the main run's aggregate_defaults.aggregate_cmd>,
    output_path  = f"_aggregated/_reducecheck/{main_run_id}-canary.json",
    timeout_sec  = 300,                        # bounded small — see cost
)
```

This works out of the box because a canary is a first-class run: `_fire_canary` +
`_mirror_canary_sidecar` leave a **journal record** (so `cluster_reduce`'s
`load_run` finds `ssh_target` + `remote_path`) and a **mirrored sidecar** carrying
`result_dir_template`, `cmd_sha`, `env`, `cluster`, `remote_path`
(`_CANARY_MIRROR_ESSENTIALS`). `cluster_reduce`:

1. threads the run's env activation via `remote_activation_for_sidecar` — the
   reducer's literal `python3` binds the **run's env interpreter**, exactly as the
   final reduce does (`cluster_reduce.py:287-299`). This is the *only* way to catch
   the run-14 py3.8-vs-3.13 class before the array runs;
2. runs the reducer with `HPC_RUN_ID=<main>-canary` + `HPC_AGGREGATED_OUTPUT=…` —
   a `run_id`-scoped reducer (the recommended convention) discovers exactly the
   canary's ONE row;
3. pulls + parses the single JSON output.

**Verify the contract SHAPE, never the values.** The check asserts only:
`exit_code == 0`, the output file exists, it parses as JSON, and it carries the
expected top-level keys (metrics parseable, expected columns present if the run
declares them). It asserts **nothing** about the numbers — a single canary row's
QLIKE/median/DM value is meaningless; correctness is rung 3's job (§3).

**Cost — apply the U4 "probe rides an existing leg" precedent.** The reducer must
`exec` python on the cluster, which cannot fold into a shell `bash` read (the same
reason `cluster_reduce` is its own ssh, and `verify_canary`'s checkpoint probe
"stays its OWN ssh"). So it is **one extra bounded ssh**. But S2 already spins a
**detached background worker to own the canary poll** (`submit_blocks.py:~829`);
append the reduce exec to that worker so it costs **no new foreground round-trip**
and runs entirely inside human-review window (b). Bound the timeout small (~300s,
not the 1800s harvest default) so a hanging reducer never stalls the S2→S3
window. On a warm-connection / same-worker leg the marginal wall-clock is ~0.

**Failure semantics — DISCLOSE, never refuse (user doctrine: gates don't tighten,
bare `y` stands).** A non-zero reducer exit, a missing output, or non-JSON output
→ a `reducer_check` **disclosure** on the S2 brief (carried to S3) naming the
error **verbatim** (`exit_code` + `stderr_tail`, which `cluster_reduce` already
surfaces as the last ~2KB). It is a loud, never-auto-masked readiness line — NOT a
hard block. The human's bare `y` still crosses it deliberately: they may intend to
register or fix the reducer between the canary and aggregate. This mirrors
finding-4's disclosed-not-blocking payload and the R1 recommendation for the S1
static disclosure.

**Severed-read semantics — UNKNOWN, never "check passed."** ssh severed / timeout
/ no output → status **`unverified`**, disclosed as "reducer check could not run
(channel severed) — UNVERIFIED," never a pass. This is the positive-evidence-only
posture: `verify_canary`'s `reporter_unreachable` never-pass-unverified arm and
`_combiner`'s `BATCH_END_SENTINEL` truncation rule are the precedents. A torn
reduce is unknown, not clean.

**What a single canary row CANNOT prove (state honestly).** The canary is task 0
of the main run — ONE row, ONE grid point, ONE arm. It cannot exercise:
- **multi-arm joins** — a sweep's cross-grid-point `pd.concat` (the reducer reads
  N task dirs; the canary offers 1);
- **cross-wave unions** — the reducer over every `_combiner/wave_*.json`;
- **pairwise statistics** — a Diebold–Mariano `dm_better` needs ≥2 models; a
  reducer that asserts `len(chunks) >= 2` will legitimately exit non-zero on one
  row. **This is a real false-alarm source**: the check must render such a failure
  as a plain disclosure ("reducer exited 1 against the single canary row: `<stderr>`")
  and let the human read it — the machinery must NOT interpret "needs more rows"
  vs "broken code"; it surfaces the verbatim stderr and stops.

So the canary check proves the reducer **LOADS, its imports resolve in the run env,
it honors the contract env vars, and it emits the contract shape against one real
row** — it does **not** prove the aggregation is correct. That residual is
explicit and belongs to rung 3.

### 2.2 The client-side variant (pure-API backends only)

The double canary already pulls the canary's task-0 `metrics.json` locally
(`_pull_canary_task0_metrics`, under `_aggregated/_fingerprints/_pulls/…`). For a
**pure-API backend** (`requires_ssh = False`), the reducer already runs on the
control plane (`local-reduce`, `$HPC_RESULTS_DIR`), so the check can run
client-side over the pulled artifact at zero ssh cost — it rides the pull the
fingerprint already paid for. For **SSH backends** this is unsound: the reducer's
deps (numpy/pandas) are pinned in the *run's cluster env*, not locally, and a
file-path reducer runs on the cluster. So: **cluster-side is primary; client-side
only when the backend already reduces locally.** The portable-reducer contract
(`$HPC_RESULTS_DIR` when set, else cluster convention) makes the SAME reducer valid
on both, so no reducer author changes.

### 2.3 The first-wave partial-harvest window (d) — a follow-on rung, not first

The reconcile tick that fires when wave 0 completes has a fuller fixture (multiple
rows, ≥1 grid point, possibly ≥2 arms) and can therefore exercise the joins the
canary row cannot. Seam: the reconcile tick (`ops/monitor/reconcile.py`) already
runs cluster-side combines; a wave-0 reducer check would call `cluster_reduce` over
the completed wave's dirs. **But** this is *after* wave-0 compute is spent — later
and more expensive than the canary rung, and it only catches join/union bugs that
slipped the canary rung. Recommend DEFER (see RC3): build only if such bugs prove
to slip in practice.

---

## 3. Interaction with the concurrent units

### 3.1 Two rungs of one ladder (the S1 static disclosure)

The canary check is the **missing middle** of a three-rung reducibility ladder —
each rung strictly stronger, each catching a class the one before cannot:

| Rung | Where | Kind | Catches | Cannot catch |
|------|-------|------|---------|--------------|
| **1** | S1 (`_reducibility_issue` / `per_task_fallback_reducible`, concurrent unit) | STATIC predicate on the sidecar | no declared path to a number (non-JSON artifact + no `aggregate_cmd`) | anything about whether a *declared* reducer runs — a custom `aggregate_cmd` passes rung 1 unconditionally |
| **2** | canary-verify (THIS unit) | DYNAMIC execution on ONE real row | reducer won't load / import-miss in run env / wrong output path / non-JSON output / crash on real data shape / non-zero exit | aggregation correctness across arms/waves |
| **3** | aggregate-check → aggregate-run | FULL integrity over all evidence | join/union/pairwise correctness; the authoritative numbers | (nothing left — this is ground truth) |

Rung 1 is a predicate ("is a reducer *declared*?"); rung 2 is a proof ("does the
declared reducer *execute*?"). They are complementary, not redundant: rung 1 fires
when NO reducer is declared, rung 2 fires when one IS declared but might be broken.
The one-definition discipline the S1 unit establishes ("the S1 predicate and the
CHECK predicate are the SAME call, they can never disagree") extends here: rung 2
runs the SAME `cluster_reduce` rung 3 runs — no rung re-derives the reduction.

There is also a **direct precedent already shipped**: canary **walltime
calibration** (`canary_calibration.py`) already takes the canary's measured
*wall-clock* and uses it to size the main array before it launches. The reducer
check is the exact analogue for the reducer's *executability* — same window, same
"canary produces real evidence that gates/sizes the main run," same one-definition
disclosure onto the S2/S3 brief.

### 3.2 The streaming custom-reducer question (R2, pending — do NOT design here)

`aggregate-stream` currently reduces a custom-reducer run through the **built-in**
mean, silently (`docs/plans/s4-gaps-2026-07-17.md` item 5; reducer-contract.md
§"Streaming does not run your reducer yet"). Its pending ruling **R2** is
refuse-to-stream vs per-arm `cluster_reduce`. **If** the per-arm-invoke path lands
later, this canary check becomes its **precondition**: you do not want to invoke an
unproven reducer N times per census tick. A reducer **proven-on-canary is the entry
bar** for per-arm streaming. Note the dependency; R2 is not designed here.

### 3.3 Concurrent editors (coordination)

- `ops/submit_blocks.py` — the S1-disclosure session is editing the S1 resolved
  leg. This unit's ONLY touch there is appending a `reducer_check` field to the S2
  brief data block (near the existing `canary_run_id` / `walltime_calibration`
  brief fields, `submit_blocks.py:~872-913`) — file-disjoint from the S1 leg but
  same file, so **sequence after** the S1 unit lands or land the brief-surface as a
  separate small edit.
- `state/*` — U3 phase-1. This unit does not touch `state/*` (it reuses
  `cluster_reduce`, which already reads the journal + sidecar). No conflict.

---

## 4. Recommendation

### 4.1 Window ranking

1. **Canary window (a)+(b) — cluster-side check at canary-verify.** BUILD FIRST.
   Earliest possible fixture (before ANY main compute), rides an existing detached
   leg (U4), reuses `cluster_reduce` verbatim (one-definition with the final
   harvest), verdict amortized into human-review dead time. Zero critical-path
   wall-clock.
2. **First-wave partial harvest (d).** Follow-on. Covers the joins the canary row
   cannot, but only after wave-0 compute — DEFER to RC3.
3. **Client-side (§2.2).** Only for pure-API backends, where it is free.
4. **S3 staging (c).** Not viable — no fresh evidence.

### 4.2 The first build — seam, surface, files, tests

**Seam / verb owner.** Extend `submit_and_verify` (the canary-verify path) with a
new best-effort helper `_check_reducer_on_canary(experiment_dir, spec,
canary_run_id)`, called right after the first canary is marked terminal
(`submit_and_verify.py:1046`) — or folded into the existing double-canary block so
it rides the same terminal read cycle. Gate it on **the run declaring a custom
`aggregate_cmd`** on its sidecar (no custom reducer ⇒ the built-in mean is
framework code, nothing to check ⇒ skip). Best-effort by contract, exactly like
`_mint_double_canary_sample`: the check NEVER fails a submit whose canary verified
ok — a raise becomes an `unverified` disclosure, never a block.

**Surface.** A `reducer_check` block on the S2 brief data (`submit_blocks.py`),
carried to S3, with `{status: passed|disclosed|unverified|skipped, exit_code,
stderr_tail, reducer_cmd}` and a code-rendered one-line disclosure the block loop
relays verbatim. A journal disclosure line (the same never-auto-mask class as the
S1 readiness disclosure) so the verdict is durable across the S2→S3 boundary.

**Files touched.**
- `src/hpc_agent/ops/submit_and_verify.py` — the `_check_reducer_on_canary` helper
  + call site (post-:1046 or folded into the double-canary block).
- `src/hpc_agent/ops/submit_blocks.py` — attach `reducer_check` to the S2 brief +
  S3 disclosure (coordinate with the concurrent S1 editor, §3.3).
- `src/hpc_agent/ops/aggregate/cluster_reduce.py` — **no change** (reused as-is;
  the check just calls it with the canary run_id + a `_reducecheck/` output path).
- `tests/ops/test_submit_and_verify.py` (or a new
  `tests/ops/test_canary_reducer_check.py`).

**Tests + fault drills owed.**
- reducer exits 0, emits valid JSON with expected keys → `passed`, no disclosure.
- reducer exits non-zero → `disclosed` with the **verbatim** `stderr_tail`, submit
  NOT refused, bare `y` still advances.
- reducer needs ≥2 rows (asserts on the single canary row) → `disclosed`, not a
  refusal (the false-alarm residual, §2.1).
- ssh severed / timeout during the reduce → `unverified`, never `passed` (fault
  drill: inject a `RemoteCommandFailed` / no-output).
- reducer emits non-JSON / missing output file → `disclosed`.
- run has NO `aggregate_cmd` (built-in path) → `skipped`, byte-identical to today.
- the check calls `cluster_reduce` (not an inlined reduction) — a one-definition
  test asserting the canary check and the final harvest share the SAME reducer
  invocation.

**Enforcement row draft** (`docs/internals/principles/determinism-boundary.md` or
the aggregate section of `lifecycle-verdicts.md`; format `| Rule | Enforced by |
Fires when |`):

```
| A declared custom reducer is EXECUTED against the canary's real output before the main array launches — the canary reducer check runs the SAME cluster_reduce the final harvest runs (one-definition), asserts only the contract SHAPE (never values), discloses any error verbatim, and never refuses a submit or reports a severed read as passed | tests/ops/test_canary_reducer_check.py::test_discloses_not_refuses, ::test_severed_check_is_unverified_not_pass, ::test_uses_cluster_reduce_not_inlined | the check inlines its own reducer invocation, hard-refuses on a reducer error, asserts a value, or reports a severed/absent read as passed |
```

**Size.** SMALL-to-MEDIUM. The reduction machinery is entirely reused
(`cluster_reduce` + the canary's journaled record + mirrored sidecar); the work is
(i) the gate on a declared `aggregate_cmd`, (ii) the best-effort helper riding the
canary worker with a bounded timeout, (iii) the brief/journal surface, (iv) the
disclose/unverified/skipped fault semantics, (v) tests + drills. No new primitive,
no `state/*` change.

### 4.3 User rulings owed (recommendation each)

- **RC1 — on-by-default for every submit with a custom reducer?** *Recommend YES,
  on by default,* gated only on a declared `aggregate_cmd`, with an env opt-out
  `HPC_NO_CANARY_REDUCER_CHECK=1` mirroring `HPC_NO_DOUBLE_CANARY`. It rides dead
  time + an existing leg at ~0 wall-clock, and a broken reducer is very expensive
  to discover at final harvest. The cost case for a per-submit opt-in is weak.
- **RC2 — disclose vs block on a failed check?** *Recommend DISCLOSE, never block.*
  Consistent with R1 (S1) and finding-4. Stronger here than at S1: an S1
  non-reducible plan can *never* produce a number, but a canary reducer-check
  failure MIGHT be a "needs ≥2 rows" false alarm (§2.1) — blocking on it would be
  actively wrong. Loud, never-auto-masked disclosure; bare `y` stands.
- **RC3 — also build the first-wave-partial-harvest rung (window d)?** *Recommend
  DEFER.* Build the canary rung first; add the wave-0 rung only if join/union bugs
  demonstrably slip past the canary rung in practice. The reconcile-tick seam
  exists but the rung adds cost after wave-0 compute.

Everything else is pure engineering.

---

## Drift log

**2026-07-17 — created (design-only).** Cites the user directive (verbatim above):
"ensure that any reduction that the LLM proposes is checked to make sure it works,
in an amortized fashion — … while we wait for the canary to run or during s3 or
something." Established, against `main @ 6d29e23b`: the canary's task-0 output is a
genuine task artifact already produced (and, for the double canary, already pulled
locally) at canary-verify time; `cluster_reduce` runs the reducer cluster-side
under the run's env-python and is directly reusable with the canary's run_id
because a canary is a first-class journaled run with a mirrored sidecar; the S2
detached canary worker is the existing leg the check rides (U4); the S2→S3 human
boundary is the dead time the check's compute amortizes against; window (c) S3
staging carries no fresh evidence (deploy ran pre-canary). Positioned the check as
rung 2 of a three-rung ladder (S1 static predicate → canary dynamic execution proof
→ aggregate full integrity), complementary to the concurrent S1 static-disclosure
unit (`docs/plans/s4-gaps-2026-07-17.md` item 1) and a precondition for the pending
R2 per-arm streaming path (item 5). Three rulings flagged: RC1 (on-by-default,
*recommend yes*), RC2 (disclose-not-block, *recommend disclose*), RC3 (also build
the wave-0 rung, *recommend defer*). No `src/**` touched; no commit. This doc is the
canonical spec home for the amortized-reduction-check unit; the reducer contract it
executes is `docs/reference/reducer-contract.md`, the reduction machinery is
`ops/aggregate/cluster_reduce.py`, and the canary machinery is `ops/verify_canary.py`
+ `ops/submit_and_verify.py`. Enforcement row named above is OWED at build time;
regen the principles index (`python scripts/regen_all.py --write`) when it lands.
