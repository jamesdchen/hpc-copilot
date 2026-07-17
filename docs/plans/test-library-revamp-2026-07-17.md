# Test-library revamp — the definitive coverage map + finishing ladder (2026-07-17)

**Status:** DRAFT / plan-only (docs-only worktree; no src, no tests, no commits).
**Author:** revamp-map session, 2026-07-17. **Ruling:** the USER ruled "finish the
full revamp of the test library." This memo is the finishing map + dispatch ladder.

**Doctrine (this session's, proven):** *behavior-pinning over line coverage.* Every
assertion names the mutation it kills; red-then-green before green-only; sensitivity
checks; **guard-can-fire** (a guard test must actually be able to trip the guard);
seam/integration tests for cross-unit dataflow; **consequence-ranked** targeting
(don't chase kill-rate uniformly — spend where a silent wrong-path is expensive).
The **false-survivor lesson** (from `mutation-triage-2026-07-17.md`) governs the
classification: *a survivor — or an apparently-untested module — is only a real gap
after checking it against the FULL covering set, not a single scoped file.* Several
modules in this map look dark by an import grep but are exercised end-to-end through a
re-exporting `__init__`; they are demoted to PARTIALLY, not called gaps.

---

## 0. Counts (task deliverable)

Module universe: **639** `src/hpc_agent/**.py`, of which **584** are non-`__init__`.
Test files: **806**. Dedicated behavior-pin batteries today: **8** `*_coverage.py` +
**14** `test_*boundary*.py` = **22**.

Classification of the **584 non-`__init__` modules** (honest, false-survivor-pruned):

| Class | Count (approx) | Meaning |
|---|---:|---|
| **PINNED** | ~30 | A dedicated behavior-pin battery (a `*_coverage.py`, a `*boundary*.py`, or a mutation-verified red-then-green set) names the mutations it kills. |
| **IN-FLIGHT** | ~35 | A concurrent agent is landing the battery this session (campaign atoms, submit_flow, announce/harvest, reproduction-act). Do **not** duplicate. |
| **PARTIALLY** | ~495 | Has unit/integration tests but **no mutation-grade behavior-pin battery**. This is the bulk of the tree and mostly acceptable steady-state — the revamp only owes batteries for the **consequence-ranked subset** below, not all 495. |
| **DARK** | ~12 | No dedicated behavioral assertions at all (thin wire queries + a few render/view modules). Mostly low-consequence projections. |
| **N/A** | ~12 | Generated scaffolds (`execution/mapreduce/templates/**`), the deployed `hpc_watcher.py`, test fixtures (`conformance/fixtures/stub_worker.py`), `__main__.py`, `_build_info.py`. Exercised (if at all) in the target repo / integration, not core-pinnable. |

**Reading of the counts:** the revamp is **not** "write 495 batteries." PARTIALLY is
the expected resting state for low-consequence code. The finishing work is the ~15
consequence-ranked PARTIAL/DARK modules in §3 — the trust/consent core, the run-record
ground truth, and the executor guard — plus wiring the measurement loop (§5) so the
claim "done" is mechanical, not asserted.

---

## 1. The LANDED behavior-pin batteries (inventory)

### 1a. Dedicated `*_coverage.py` batteries (this session's core)

| Battery | Subject module | Kills / pins |
|---|---|---|
| `tests/state/test_journal_coverage.py` | `state/journal.py` | journal append/read, hygiene, idempotence |
| `tests/state/test_index_coverage.py` | `state/index.py` | run-index reshape / lookup branches |
| `tests/state/test_attestation_coverage.py` | `state/attestation.py` | validate/bind/reduce kernel (mutation-verified, commit `3223e675`) |
| `tests/ops/test_block_chain_coverage.py` | `infra/block_chain.py` | successor tables + spec composition (45 pins, `3223e675`) |
| `tests/ops/decision/test_authorship_coverage.py` | `ops/decision/journal/_shared.py` (+ bare-ack) | `_BARE_ACK_RESPONSES` membership, authorship gate literals (130 edge pins, `94297ba7`) |
| `tests/ops/decision/test_verify_relay_coverage.py` | `ops/decision/journal/verify_relay.py` | relay numeric trust core |
| `tests/ops/monitor/test_reconcile_recovery_coverage.py` | `ops/monitor/reconcile.py` | reconcile recovery ladder (21 pins, `3223e675`) |
| `tests/_kernel/lifecycle/test_consent_hint_coverage.py` | `_kernel/lifecycle/consent_hint.py` | consent-hint seam |

### 1b. Boundary batteries (`tests/contracts/test_*boundary*.py`)

Per-verb behavior boundaries (input/output envelope + refusal paths):
`attestation_export`, `challenge`, `cite_check`, `conformance`, `determinism`,
`dossier`, `evidence`, `extract_recipe`, `multi_human`, `pack`, `registration`,
`run_story`, plus the generic `boundary_contract` and `lint_backend_boundary`.
(The **`publication_bundle`** boundary battery is landing via a concurrent
feature agent — see git status `tests/contracts/test_publication_bundle_boundary.py`.)

### 1c. The three sweep-gap pins (from `mutation-triage-2-2026-07-17.md` drift log)

Mutation-verified red-then-green against faithful mutated copies (no src touched):

| Battery | Subject | Mutants killed |
|---|---|---|
| `tests/ops/submit/test_effective_backend_cap.py` | `submit_flow._effective_cap_for_backend_name` | min-of-two-ceilings cap (8) |
| `tests/infra/test_pull_disclose.py` | `transport/_pull._disclose_pull_batch` | exact disclosure line, MiB divisor, stderr (6) |
| `tests/ops/aggregate/test_incremental_include_patterns.py` | `aggregate_flow._incremental_include_patterns` | empty-waves gate, anchored regex, glob pair (7) |

### 1d. Adjacent hardened batteries (behavior-pin grade, not named `_coverage`)

`tests/ops/test_decision_journal_primitives.py`, `tests/state/test_determinism.py`,
`tests/ops/monitor/test_reconcile_*` (announce/canary/failed/kill/stale/submitting),
`tests/faultinject/**` (breaker, delta-push, stage-swap atomicity, submit-once,
transfer-pipe), `tests/ops/submit/test_canary_*` (calibration, gate, crash-window,
terminal-transition), `tests/integration/test_reproducibility_chain_e2e.py` (the
"stranger re-derives the table" seam standard, `da990704`).

---

## 2. IN-FLIGHT (concurrent agents — do NOT duplicate)

Marked in-flight per the task brief; these modules are being pinned in parallel:

| In-flight battery | Subject modules |
|---|---|
| **campaign atoms** | `meta/campaign/atoms/*` (advance, budget, circuit_breaker, compute_spend, converged, decide_concurrency, health, resubmit_cap, replay, load_context, acknowledge_budget), `meta/campaign/{blocks,cursor,manifest,budget_ack}.py` |
| **submit_flow** | `ops/submit_flow.py`, `ops/submit/runner.py`, `ops/submit_pipeline.py` |
| **announce / harvest** | `ops/monitor/announce.py`, `ops/monitor/harvest_guard.py`, `ops/migrate/harvest.py`, `ops/monitor/reconcile_stale.py` |
| **reproduction-act** | `ops/reproduce_run.py`, `ops/decision/journal/reproduction.py`, `ops/verify_reproduction.py` |

Note: `ops/decision/journal/reproduction.py` is *also* a Wave-1 trust-core target in §4
— coordinate: if the in-flight reproduction-act agent lands its authorship battery,
Wave-1 drops that module.

---

## 3. Consequence-ranked PARTIAL + DARK (verified honestly)

Ranking: **trust/consent > data-integrity > transport/actuation > lifecycle verdicts >
projections/renders > devx.** Each row verified against its true covering set (not an
import grep). "Incidental" = touched by cross-module tests but no test asserts *its*
branches; "e2e-only" = exercised end-to-end via a re-exporting `__init__`, branches
unpinned.

### Trust / consent (top tier)

| Module | Lines | Honest state | Why it ranks |
|---|---:|---|---|
| `ops/decision/journal/signoff.py` | 661 | **PARTIALLY** — e2e via `test_multi_human_gate.py`; no per-branch battery | multi-human sign-off / consent commit; a wrong-path forges consent |
| `ops/decision/journal/challenge.py` | 485 | **PARTIALLY** — e2e via `test_challenge_authorship.py`; view-sha recompute unpinned | challenge filing/verdict authorship + tamper detection |
| `ops/decision/journal/conclusion.py` | 250 | **PARTIALLY** — e2e via `test_conclusion_authorship.py`; revoke-floor thinly pinned | conclusion authorship + revoke floor |
| `ops/decision/journal/reproduction.py` | 224 | **PARTIALLY / IN-FLIGHT** | reproduction-verdict authorship (see §2) |
| `ops/decision/journal/overnight_consent.py` | 200 | **PARTIALLY** — e2e via `test_overnight_consent.py`; compose branches unpinned | overnight autonomy consent — the highest-blast consent path |
| `ops/decision/journal/human_authorship.py` | 212 | **PARTIALLY** — `_assert_human_authorship` gate hit e2e; literal drift unpinned | the human-authorship gate itself |
| `ops/decision/journal/brief_provenance.py` | 138 | **PARTIALLY** — e2e only | brief provenance stamping |

### Data-integrity

| Module | Lines | Honest state | Why it ranks |
|---|---:|---|---|
| `state/run_record.py` | 605 | **PARTIALLY** — incidental across 10 state tests; **no dedicated battery** | the run sidecar = ground truth; field ownership + terminal transitions decide every downstream verdict |
| `state/runs.py` | 1515 | **PARTIALLY** — many `test_runs_*` but no unified boundary battery; largest module in tree | run lifecycle store |
| `state/data_manifest.py` | 439 | **PARTIALLY** — `test_data_manifest.py` (19 tests), not mutation-grade | data-trace manifest = reproducibility data leg |
| `infra/executor_guard.py` | 685 | **PARTIALLY** — incidental via `test_executor_env_guards.py` + `test_write_run_sidecar.py` only | the guard that validates an executor **before** it burns cluster time; classic guard-can-fire target |

### Transport / actuation

| Module | Lines | Honest state | Note |
|---|---:|---|---|
| `infra/ssh_circuit.py` | 703 | **PARTIALLY (better than it looks)** — `test_ssh_circuit.py` has **118 asserts**, 20 mocks | the breaker internals; strong unit coverage, but state-transition/threshold boundaries not mutation-pinned. Verify before building — likely a *thin* battery, not a full one. |
| `infra/io.py` | 603 | **PARTIALLY** — `test_io_durability.py` + `test_atomic_write_{json,text}.py` + `test_atomic_locked_update.py` | locks + atomic writes; solid, add threshold/boundary pins only |
| `infra/transport/_excludes.py` | 234 | **PARTIALLY** — `test_pull_dest_excludes.py` | exclude-glob logic |
| `infra/backends/_scripts.py` | 195 | **PARTIALLY** — render-golden tests | job-script assembly |

### Lifecycle verdicts / hooks-beyond-relay / cli

| Module | Lines | Honest state |
|---|---:|---|
| `cli/_dispatch.py` | 466 | **PARTIALLY** — `test_dispatch.py`, `test_fast_dispatch.py`, `test_cli_dispatcher_inline_parity.py`; verb→module resolution not boundary-pinned |
| `_kernel/hooks/scheduler_write_fence.py` / `stop_multiplex.py` / `skill_return_*` | 60–200 | **PARTIALLY** — each has a `test_*`, none mutation-grade |
| `_kernel/hooks/relay_audit_stop/{_echo,_decision_state,_relay_due,_paraphrase}.py` | 85–216 | **PARTIALLY** — e2e via `test_relay_audit_stop.py` (the "beyond relay" note: relay itself is covered, its sub-parts are e2e-only) |

### Genuinely DARK (no behavioral assertions — mostly low consequence)

`_wire/queries/failures.py` (74), `_wire/queries/suggest_setup_action.py` (30),
`ops/registration_view.py` (28), `ops/notebook_view.py` (50) — thin projections/renders.
`meta/campaign/atoms/compute_spend.py` (170) is DARK by grep but **IN-FLIGHT** (campaign
atoms). None are consent- or data-bearing; accept as-is or pin opportunistically.

### Top-5 DARK/PARTIAL by consequence (deliverable)

1. **`ops/decision/journal/signoff.py`** — multi-human sign-off / consent commit (trust).
2. **`ops/decision/journal/challenge.py`** — challenge authorship + view-sha tamper (trust).
3. **`infra/executor_guard.py`** — pre-run executor validation; guard-can-fire (data-integrity/actuation).
4. **`ops/decision/journal/{conclusion,reproduction,overnight_consent}.py`** — authorship/consent commit paths (trust/consent).
5. **`state/run_record.py`** — the run-sidecar ground truth; field ownership + terminal transitions (data-integrity).

---

## 4. The finishing DISPATCH LADDER (file-disjoint waves)

Each unit is a **new** `*_coverage.py` battery → writes are inherently file-disjoint,
so every wave dispatches as parallel Opus agents (one per battery). Every unit follows
the doctrine: red-then-green against a faithful mutated copy, sensitivity check,
guard-can-fire. **No src edits** — batteries only.

### Wave 1 — trust/consent core  ★ highest

| Unit | New file | Subject | Key boundaries to pin | Size |
|---|---|---|---|---|
| 1a | `tests/ops/decision/test_signoff_coverage.py` | `journal/signoff.py` | multi-human quorum gate, echo/self-approval detection, bare-ack rejection, sign-off sha recompute | M |
| 1b | `tests/ops/decision/test_challenge_coverage.py` | `journal/challenge.py` | filing vs verdict authorship split, `_recompute_challenge_view_sha` tamper, citation/attestor requireds | M |
| 1c | `tests/ops/decision/test_consent_commit_coverage.py` | `journal/{conclusion,overnight_consent}.py` | conclusion authorship + revoke-floor, overnight consent compose branches (the highest-blast consent path) | M |

(Wave 1 drops `journal/reproduction.py` — the in-flight reproduction-act agent owns it.
Coordinate at collect time; if it slips, add unit 1d `test_reproduction_authorship_coverage.py`.)

### Wave 2 — data-integrity  ★ high

| Unit | New file | Subject | Key boundaries | Size |
|---|---|---|---|---|
| 2a | `tests/state/test_run_record_coverage.py` | `state/run_record.py` | sidecar field ownership matrix, terminal-transition legality, version-mismatch read-compat | M |
| 2b | `tests/infra/test_executor_guard_coverage.py` | `infra/executor_guard.py` | **guard-can-fire**: each rejection path can actually trip; the guard is not a no-op under a malformed executor | M |
| 2c | `tests/state/test_data_manifest_coverage.py` | `state/data_manifest.py` | deepen the 19-test file: manifest field-derivation + data-env-sha boundaries | S |

### Wave 3 — transport / actuation  ★ med (verify-before-build; may be thin)

| Unit | New file | Subject | Key boundaries | Size |
|---|---|---|---|---|
| 3a | `tests/infra/test_ssh_circuit_coverage.py` | `infra/ssh_circuit.py` | breaker state-machine transitions (closed→open→half-open), threshold constants (off-by-one), backoff arithmetic. **Verify first** — 118 asserts exist; this may be a *thin* top-up, not a full battery. | S–M |
| 3b | `tests/infra/test_io_boundary.py` | `infra/io.py` | atomic-write failure/rollback, lock-contention path (already partly covered — top-up only) | S |

### Wave 4 — lifecycle verdicts / hooks / cli  ★ med-low

| Unit | New file | Subject | Key boundaries | Size |
|---|---|---|---|---|
| 4a | `tests/cli/test_dispatch_coverage.py` | `cli/_dispatch.py` | verb→module resolution table, fast-path vs full-path parity, unknown-verb refusal | S |
| 4b | `tests/_kernel/hooks/test_hooks_beyond_relay_coverage.py` | `hooks/{scheduler_write_fence,stop_multiplex,skill_return_stop_guard}.py` | fence trip conditions, stop-multiplex arbitration, return-stop guard-can-fire | M |

### Wave 5 — projections / renders / devx  ★ lowest (opportunistic)

The genuinely-DARK thin renders (`registration_view`, `notebook_view`,
`_wire/queries/{failures,suggest_setup_action}`). **Accept as-is** unless a render
becomes load-bearing; not worth a battery under consequence-ranked doctrine.

**Dispatch classification (per user's parallelization rule):** Waves 1–4 are each
`[∥]` internally (disjoint new files, one Opus agent per unit). Waves run `[seq]`
relative to each other only to keep the collect/measurement checkpoint clean; there is
no data dependency, so an aggressive coordinator MAY fan all of Waves 1–4 at once
(11 disjoint files, zero conflict frontier).

---

## 5. MEASUREMENT plan — how "done" is proven mechanically

The revamp is **DONE** when, for the curated modules, the **genuine**-survivor count is
~0 and the sweep's survivors are triaged (scoping-artifact vs genuine). That claim must
be produced by the repaired mutation workflow, not asserted.

### Preconditions (already landed — verify, don't rebuild)

Per `mutation-triage-2-2026-07-17.md` drift log, the curated regression is **fixed**
(commit `fb481044`, in `62cb0a5a`): relative `paths_to_mutate` restored, per-module
chdir deselects, `mutmut==3.6.0` pinned, curated tripwire gating on `killed+survived>0`,
describe-cache baseline skip. **But the fixed re-dispatch is still OWED** — the last
recorded run (`29601496071`) executed the *pre-fix* curated matrix (all 10 dark). So:

### Steps

1. **Re-dispatch off current `main`** — `gh workflow run mutation.yml`. This is the
   first run that should yield real curated + core-seam data. Confirm the curated
   tripwire reads honest-green (`killed+survived>0`), not zero-signal-green.
2. **Add the new Wave-1..4 subject modules to `scripts/run_mutation.py::MODULE_MAP`**,
   each paired with its true covering set (the new `*_coverage.py` battery **plus** the
   pre-existing e2e tests — the covering set, not a single file, per the false-survivor
   lesson). New keys: `signoff`, `challenge`, `consent-commit`, `run-record`,
   `executor-guard`, `data-manifest`, `ssh-circuit`, `cli-dispatch`.
3. **Compare genuine-survivor count per module vs the 2026-07-17 baselines:**
   - Sweep baseline (`mutation-triage-2-2026-07-17.md`, run `29601496071`):
     `submit_flow` 37% kill-of-covered, `aggregate_flow` 21%, `transport/__init__` 43%,
     `_pull` 61%, `_combiner` 59%. Target: sweep genuine survivors triaged, not
     necessarily zero (SSH-bound survivors are scoping artifacts by construction).
   - Curated core-seam baseline: **EMPTY** (`state/journal`, `state/index`,
     `decision-journal`, `consent-hint` produced zero data — the matrix was dark). The
     re-dispatch produces the **first-ever** core-seam numbers; the new batteries must
     drive those keys' genuine-survivor count to ~0.
4. **Triage each survivor** per `docs/internals/mutation-testing.md` checklist:
   TEST-GAP → build a pin; EQUIVALENT-MUTANT / LOW-VALUE → document and move on. Record
   the disposition (don't build off raw counts — the false-survivor lesson).
5. **Certificate of done:** a triage memo (`mutation-triage-3-YYYY.md`) showing, per
   curated key, genuine-survivor = 0 (or documented-equivalent), and the sweep survivor
   set fully bucketed. Until that memo exists, the revamp is "batteries landed, not yet
   measured-done."

### Open dependency

The measurement loop is gated on the **owed re-dispatch** (step 1). If a session lands
Waves 1–4 but cannot re-dispatch (do-not-push session), it must record the batteries as
**landed-unmeasured** and hand step 1 to the next session — mirroring exactly how
triage-1 handed the fix-forward to triage-2.

---

## 6. Drift log

- **2026-07-17** — first definitive test-library coverage map. Universe: 639 src
  modules (584 non-init), 806 test files, 22 dedicated behavior-pin batteries (8
  `*_coverage` + 14 boundary). Classification: ~30 PINNED / ~35 IN-FLIGHT / ~495
  PARTIALLY / ~12 DARK / ~12 N/A. **Headline:** the tree is broadly *tested* but
  narrowly *behavior-pinned*; the finishing work is the ~15 consequence-ranked
  PARTIAL/DARK modules, not a blanket sweep. **Top-5 dark-by-consequence:**
  signoff, challenge, executor_guard, {conclusion/reproduction/overnight_consent},
  run_record. **False-survivor pruning applied:** `elision`, `_classifier`,
  `_kernel/decision/kernel`, `pandas_rolling`, and the entire `decision/journal/*`
  family were rescued from a naive import-grep "dark" verdict — they are e2e-exercised
  through re-exporting `__init__`s and are PARTIALLY, not gaps. `ssh_circuit` flagged
  as a task suspect turned out **better-covered than it looks** (118 asserts) — Wave 3
  marks it verify-before-build.
- **Ladder:** 5 waves, 11 disjoint new-file battery units (Waves 1–4 buildable now;
  Wave 5 declined under consequence-ranked doctrine). Wave 1 = the trust/consent core
  (signoff / challenge / consent-commit).
- **Measurement gate:** the "done" certificate is a mutation-triage-3 memo, gated on
  the **owed re-dispatch off main** (the triage-2 fix-2 is committed but never re-run
  fixed). Batteries landed without that re-dispatch are "landed-unmeasured."
- **Coordination note:** `decision/journal/reproduction.py` is claimed by both this
  ladder (Wave 1d fallback) and the in-flight reproduction-act agent — de-dup at
  collect time; the in-flight owner wins.
- **Open question for the operator:** should Wave 5 (thin renders/wire-queries) ever be
  pinned, or is "PARTIALLY/DARK but low-consequence" the accepted terminal state for
  projection code? Recommend the latter — a render battery pins string literals that the
  `test_lint_skill_md_literal_drift` / prose-drift lints already guard more cheaply.
