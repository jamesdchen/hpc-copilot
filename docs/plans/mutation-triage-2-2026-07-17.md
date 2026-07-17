# Mutation-testing triage #2 — run 29601496071 (2026-07-17)

**Author:** triage session, 2026-07-17. **Status:** second triage; first run on the
REPAIRED harness (Units A–E, committed `e86f2d92`, pushed in `62cb0a5a`).
**Deliverable:** this memo. **Predecessor:** `docs/plans/mutation-triage-2026-07-17.md`
(run `29560911639`), whose taxonomy (TEST-GAP / EQUIVALENT-MUTANT / LOW-VALUE) and
**false-survivor lesson** (a survivor is only a gap after checking it against the
FULL covering set, not the scoped `tests_dir`) are applied throughout.

---

## TL;DR — the harness is now HALF-fixed: the sweep works, the curated matrix went fully dark

Two opposite outcomes stack, and the SUCCESS/green conclusion hides the bad half:

1. **Unit A WORKED — the scheduled sweep now checks every mutant.** All **6299/6299**
   sweep mutants were checked (prior run: **0/6076**). Real signal: **1935 killed,
   2945 survived, 1417 no-tests, 2 timeout**. The zero-signal tripwire fired and passed
   honestly (`"6299 checked / 6299 generated … tripwire OK"`). This is the concrete win:
   submit_flow / aggregate_flow / transport now produce citable mutation data.

2. **The curated matrix REGRESSED to zero — all 10 modules produced NO verdicts.**
   Nine modules abort with mutmut's `"Stopping early, because we could not find any test
   case for any mutant"`; describe-cache aborts earlier on a genuine baseline test
   failure. **Root cause = Unit B.** `run_mutation.render_scoped_pyproject` now writes
   `paths_to_mutate` as an **absolute** path (`(REPO_ROOT/scope.source).resolve().as_posix()`,
   `scripts/run_mutation.py:292`) to dodge the combiner chdir crash. Under **mutmut 3.6.0**
   (installed UNPINNED) the absolute path breaks the stats-phase coverage attribution, so
   mutmut believes no test covers the mutated module and aborts before checking a single
   mutant. The sweep escapes this because `mutmut_shortlist.py:415` writes `paths_to_mutate`
   **relative** (`p.relative_to(REPO_ROOT).as_posix()`). One line, opposite path styles,
   opposite outcomes.

3. **First-ever core-seam data (state/journal, state/index, decision-journal,
   consent-hint) = EMPTY.** Unit D correctly added these four as curated `MODULE_MAP` keys,
   but curated is dark, so the trust core produced **zero mutation signal**. The task's
   central ask — first mutation data on the consent/journal core — **cannot be answered by
   this run.** It is gated on fixing the Unit-B regression.

**Net:** the sweep gives a large real survivor set (transport + the two flows); the core
seams give nothing. Per the false-survivor lesson the sweep survivors are scoped to a
deliberately NARROW in-process covering set, so most high-count survivors are scoping
artifacts — but a handful of pure-logic functions are covered-but-never-asserted with
**zero test naming them anywhere**, and those are the genuine gaps (§Top-3).

---

## Run provenance

| Field | Value |
|---|---|
| Run id | `29601496071` |
| Workflow | `.github/workflows/mutation.yml` |
| Trigger | `workflow_dispatch` |
| Conclusion | success (green) |
| Harness commit | `e86f2d92` (Units A–E), pushed in `62cb0a5a` |
| mutmut version | **3.6.0** (installed UNPINNED: `pip install -e '.[dev]' mutmut`) |
| Jobs | `sweep` (13m40s) + `curated-modules` matrix (11 keys) |
| Artifacts | `mutation-sweep` (+ `.meta` exit codes), 10 curated `mutation-<key>.txt` |

---

## Harness-fix verification (task step 1)

| Fix | Unit | Verdict | Evidence |
|---|---|---|---|
| Sweep checks > 0 | A | **WORKED** | 6299/6299 checked; tripwire "OK"; metas hold 0/1/33 exit codes |
| Zero-signal tripwire | A | **WORKED (with a caveat)** | fired, counted 6299 checked, passed. Caveat: it counts exit-33 "no tests" as *checked* — 1417 of the 6299 carry zero mutation signal. A future all-`no-tests` sweep would still read green. Refine to `killed+survived > 0`. |
| combiner crash fixed | B | **MOOT / still zero** | no longer the old `FileNotFoundError: 'src'`, but now aborts via the new "no test case" stop-early. Absolute-path fix *caused* that abort. |
| fast-path-cache crash fixed | B/C | **MOOT / still zero** | same stop-early abort; the re-pair never gets exercised. |
| sweep `tests_dir` scoping | A | **WORKED** | relative paths → coverage attributed → mutants checked |
| Broadened covering sets kill old false survivors | C | **UNANSWERABLE** | curated dark; block_chain/attestation/describe_cache produced no verdicts to compare against the prior run |
| Core seams added to scope | D | **added but produced ZERO data** | 4 keys present in the matrix, all abort |

**Finding #1 (the headline):** Unit B's absolute-`paths_to_mutate` render
(`scripts/run_mutation.py:292`) is incompatible with mutmut 3.6.0's stats-phase coverage
matching and zeroes out the **entire curated matrix**. Corroborating detail: curated mutant
keys are stamped with the absolute path
(`.home.runner.work.hpc-copilot.hpc-copilot.src.hpc_agent.state.attestation.x_validate__mutmut_54`)
whereas sweep keys are clean module paths
(`hpc_agent.infra.transport._combiner.x_run_combiner_checked__mutmut_1`). The absolute path
pollutes the mutant identity and defeats the coverage join. **Fix: render curated
`paths_to_mutate` relative (like the sweep) and solve the combiner chdir crash the other way
— `monkeypatch.chdir` teardown in the end-to-end test, or an absolute `source_paths` via
mutmut's own config, not via `paths_to_mutate`.** Also **pin mutmut** (`mutmut==3.6.0`) so an
upstream bump can't silently re-shape the run again. This gates everything below.

**Finding #1b (describe-cache, distinct):** describe-cache aborts even earlier — a *clean-
source* baseline failure: `tests/cli/test_describe_cache.py::test_load_path_drags_no_heavy_imports`
fails inside mutmut's chdir'd `mutants/` tree because cold `describe_cache.load()` eagerly
imports `importlib.metadata._adapters`. mutmut's `"failed to collect stats. runner returned 1"`.
This is independent of Finding #1 and will still bite after the path fix. The test's no-heavy-
import assertion does not hold under the `mutants/` layout (the trampoline shim drags the
import); make the assertion robust to the mutmut tree or deselect it from the covering set.

**Finding #2 (curated reads green while dark):** the tripwire guards ONLY the sweep. All 10
curated jobs aborted with zero verdicts and concluded **green** (mutmut's "survivors ≠ failure"
semantics + no per-curated tripwire). The exact zero-signal-green failure Unit A was built to
prevent is still live for the curated matrix. Add the same `checked > 0` tripwire to
`run_mutation.py` / the curated step.

---

## Sweep results — the only real signal this run produced

Verdicts from the uploaded `mutants/**/*.meta` `exit_code_by_key` (0 = survived, 1 = killed,
33 = no tests, −24 = timeout), cross-checked against `mutation-results.txt`.

| Module | Mutants | Killed | Survived | No-tests | Timeout | Kill-rate (of covered) |
|---|---:|---:|---:|---:|---:|---:|
| `ops/submit_flow.py` | 2244 | 707 | 1197 | 340 | 0 | 37% |
| `ops/aggregate_flow.py` | 1900 | 201 | 776 | 923 | 0 | 21% |
| `infra/transport/__init__.py` | 1093 | 451 | 594 | 48 | 0 | 43% |
| `infra/transport/_pull.py` | 863 | 521 | 340 | 0 | 2 | 61% |
| `infra/transport/_combiner.py` | 199 | 55 | 38 | 106 | 0 | 59% |
| **Total** | **6299** | **1935** | **2945** | **1417** | **2** | — |

**Scoping caveat (false-survivor lesson):** the sweep's `tests_dir` is `CLUSTER_VERB_TESTS`
— a deliberately NARROW in-process (no-SSH) covering set. The full covering tests for these
seams are integration tests that need a live cluster and are deselected in CI. So the
high-count survivors on SSH-bound functions are **scoping artifacts, not suite gaps**:
`_tar_ssh_push` (208 surv), `_submit_one_spec` (197), `_mirror_canary_sidecar` (112),
`_pull_transfer` (97), `_aggregate_flow_impl` (502) etc. are covered-and-killed only by
live-cluster tests that can never run here. Do **not** build tests off the raw survivor
counts. The genuine gaps are the pure-logic functions that are (a) reached by the in-process
set, (b) 100% survived, and (c) named by no test file anywhere.

---

## Top-3 genuine test-gap build units

Filter: `survived > 0 AND killed == 0 AND no-tests == 0` (covered-but-never-asserted), then
grep the FULL `tests/` tree by symbol to separate genuine gaps (0 references) from
scoping artifacts (referenced elsewhere). Ranked by seam risk.

### Unit 1 — `submit_flow._effective_cap_for_backend_name`  ★ high (cluster impact)
- **Signal:** 19/19 survived, 0 killed, **0 test files reference it.** Pure backend→concurrency-
  cap resolution. A wrong cap silently over- or under-submits against a real scheduler.
- **Missing assertion:** for each backend name (and the default/unknown path), `store`-free
  unit asserting the returned cap equals the expected per-backend value, and that an unknown
  backend takes the documented default (not a mutated constant/branch).
- **File:** `tests/ops/submit/` (new focused test, in-process, no SSH).
- **Size:** S.

### Unit 2 — `transport/_pull._disclose_pull_batch`  ★ med-high (consent-adjacent)
- **Signal:** 12/12 survived, 0 killed, **0 test files reference it.** Builds the human-facing
  pull-batch disclosure string — the consent/disclosure surface ranks above renders per the
  task's risk ordering, and nothing pins its content.
- **Missing assertion:** given a known batch (counts / mode / paths), assert the disclosure
  text contains the load-bearing fields; a mutated field/format must flip the assertion.
- **File:** `tests/infra/` (or `tests/ops/aggregate/`), in-process.
- **Size:** S.

### Unit 3 — `aggregate_flow._incremental_include_patterns`  ★ med (data-correctness)
- **Signal:** 14/14 survived, 0 killed; **referenced by one test file**
  (`tests/ops/aggregate/test_flow_incremental_pull.py`) that was NOT in the sweep set and
  whose assertions are evidently too shallow to kill any mutant. Incremental-harvest include
  globs decide which result files get pulled — a wrong pattern silently drops or over-pulls
  data.
- **Missing assertion:** strengthen `test_flow_incremental_pull.py` to assert the EXACT
  include-pattern list for a representative incremental spec (not just that a pull happened),
  so a mutated glob/boundary flips it.
- **File:** `tests/ops/aggregate/test_flow_incremental_pull.py`.
- **Size:** S. Lower confidence than 1–2 (has some existing coverage) — verify red-then-green.

**Also-covered-but-referenced (scoping artifacts, not building):** `_msys_local` (24 surv, in
`test_remote_*`), `_stage_swap_cmd` (4, in `faultinject/test_stage_swap_atomicity.py`),
`_canary_decision` (35, in 3 canary tests), `_combiner_only_reduce` (80, in aggregate tests),
`_reduce_input_provenance` (63, in `test_reduce_provenance_fields.py`) — all named by
out-of-sweep tests; likely killed by the full set. Re-check only after the curated path fix
gives per-module full-covering-set runs.

---

## Comparison to the prior run (task step 4)

| Dimension | Run 29560911639 (a0dd47dd) | Run 29601496071 (e86f2d92) | Delta |
|---|---|---|---|
| Sweep checked | **0 / 6076** | **6299 / 6299** | FIXED (Unit A) |
| Curated modules producing verdicts | 3 of 5 (attestation, block_chain, describe_cache) | **0 of 10** | REGRESSED (Unit B) |
| Curated crashes | 2 (combiner FileNotFound, fast-path force-fail) | 10 (all stop-early / baseline) | worse mechanism, same net zero |
| Core seams in scope | none | 4 added, **0 data** | scoped but dark |
| Did broadened covering sets kill old false survivors? | — | **UNANSWERABLE** (curated dark) | pending fix |
| mutmut version | (unrecorded) | **3.6.0**, unpinned | new deprecations bit |

The prior run's Unit-A/E work paid off (sweep + tripwire). The prior run's Unit-B/C/D curated
work is entirely un-validated because a single Unit-B path-style choice zeroes the matrix.

---

## Drift log

- **2026-07-17** — second triage; run `29601496071` @ harness `e86f2d92` (Units A–E), mutmut
  **3.6.0** unpinned. Headline: **sweep FIXED (6299/6299 checked, tripwire honest-green);
  curated matrix REGRESSED to zero (all 10 abort).** Root cause = Unit B's absolute
  `paths_to_mutate` (`run_mutation.py:292`) vs mutmut-3.6.0 stats-phase coverage matching;
  the sweep survives because `mutmut_shortlist.py:415` keeps paths relative. describe-cache
  additionally baseline-fails on `test_load_path_drags_no_heavy_imports` under the `mutants/`
  tree. First-ever core-seam (journal/index/decision/consent) data is EMPTY — the task's
  central ask is gated on the fix. Sweep produced a large real survivor set but it is scoped
  to a narrow in-process covering set (false-survivor lesson); three pure-logic, zero-test
  gaps extracted (`_effective_cap_for_backend_name`, `_disclose_pull_batch`,
  `_incremental_include_patterns`). Artifacts downloaded to a scratch dir, not committed;
  workflow retains them 30 days.
- **Next-session queue (do-not-build-here session — this memo is docs-only):**
  1. **Render curated `paths_to_mutate` RELATIVE** (mirror the sweep); fix the combiner chdir
     crash via test-side `monkeypatch.chdir` teardown, not an absolute mutate path. Gates all
     core-seam data.
  2. **Pin `mutmut==3.6.0`** in the dev extra + workflow so an upstream bump can't re-shape runs.
  3. **Fix describe-cache baseline** (`test_load_path_drags_no_heavy_imports` under `mutants/`).
  4. **Add a `checked > 0` tripwire to the curated step** (Finding #2) and refine the sweep
     tripwire to `killed+survived > 0` so exit-33 "no tests" can't fake signal.
  5. **Re-dispatch off current `main`** — only then does the curated core-seam matrix and the
     old-false-survivor comparison become answerable.
- **Open question for the operator:** curated jobs currently burn 10 matrix runners for zero
  signal and read green — pause the curated matrix (or gate it on queue-item 1) until the path
  fix lands? The sweep alone still yields real signal weekly.

- 2026-07-17 (integration) — the top-3 covered-but-unasserted gaps PINNED:
  `test_effective_backend_cap.py` (8 — the min-of-two-ceilings cap logic),
  `test_pull_disclose.py` (6 — the exact human-facing batch disclosure line,
  binary-MiB divisor, stderr), `test_incremental_include_patterns.py` (7 — the
  empty-waves gate, anchored wave regex, ordered glob pair). Sensitivity proven
  against 9 representative mutations (all killed) via faithful mutated copies;
  no src touched. Built in an isolated worktree, integrated by the coordinator.

- 2026-07-17 (fix-2, integrated) — the curated regression FIXED: relative
  paths_to_mutate restored (the absolute path poisoned mutmut 3.6.0's
  coverage-join key derivation); the combiner chdir crash re-solved the RIGHT
  way — mutmut's record_trampoline_hit resolves relative source paths against
  the LIVE cwd on every mutated call, so the chdir'ing IN-PROCESS tests are
  --deselect'ed per-module (combiner e2e classes + one journal cwd test; a
  unit test pins every deselect node-id exists so a typo can't silently
  re-crash a module); mutmut==3.6.0 pinned via a [mutation] extra; the
  curated matrix gains its own NON-continue-on-error tripwire and both
  tripwires now gate on killed+survived>0 (an all-exit-33 run can't fake
  signal); describe-cache baseline skip under the mutants tree. 47 script
  tests red-then-green; re-dispatch off main owed to finally read the
  core-seam data.
