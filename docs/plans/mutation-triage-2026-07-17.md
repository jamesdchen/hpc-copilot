# Mutation-testing triage — run 29560911639 (2026-07-17)

**Author:** triage session, 2026-07-17. **Status:** first-ever triage of the B4
mutation runner (commit `a0dd47dd`). **Deliverable:** this memo.

---

## TL;DR — the run produced almost no trustworthy signal

The 2026-07-17 `workflow_dispatch` mutation run **completed green but is nearly
empty of actionable signal.** Three independent failures stack:

1. **The scheduled cluster-verb sweep — the entire point of B4 (submit_flow /
   aggregate_flow / transport) — checked ZERO mutants.** All **6076** generated
   mutants report `not checked`. The high-severity paths where a silent
   wrong-path costs real cluster time got **no coverage at all.** (Finding 1.)
2. **Two of five curated modules crashed to zero results.** `combiner` (the
   module that computes every aggregate number — the single highest-value
   curated target) died with a mutmut infra `FileNotFoundError: 'src'`;
   `fast-path-cache` died with mutmut's `Unable to force test failures` baseline
   abort. Neither produced a single mutant verdict. (Finding 3.)
3. **The three curated modules that DID run report survivors that are mostly
   scoping artifacts, not suite gaps.** The curated runner scopes `tests_dir` to
   ONE focused test file per module, but every curated module has real coverage
   spread across OTHER test files (`tests/contracts/…`, `tests/cli/test_describe.py`,
   …) that the sweep excluded. Verified concretely for `block_chain`: its 166
   "no tests" + 17 `_wrap_run_id_under`/`_complete_spec_hint` survivors are all
   killed by `tests/contracts/test_spec_hint_completeness.py`, which was not in
   scope. (Finding 4.)

**Net:** of ~43 raw survivors, after removing scoping artifacts and infra
crashes, there is **no high-confidence, suite-level test-gap** this run can name.
The real deliverable is the **finding docket below** — the harness/scope defects
that must be fixed before ANY future mutation run yields a citable gap. Do not
build tests off the raw survivor list; it overstates gaps.

---

## Run provenance

| Field | Value |
|---|---|
| Run id | `29560911639` |
| Workflow | `.github/workflows/mutation.yml` |
| Trigger | `workflow_dispatch` (default inputs, `changed_since=""`) |
| Created | 2026-07-17 06:44:34 UTC |
| Conclusion | success (green — survivors are non-blocking by design) |
| **Mutated sha** | `e41f25e20b4fb3a77c01624ff6f52dc296681a0d` |
| Sha position | ancestor of `main` (`a01d3356`), **~31 commits behind** |
| Jobs | `sweep` (scheduled-style), `curated-keys`, `curated-modules` matrix (5 keys) |

**Sha staleness note:** the mutated sha predates `d579b3a1` (capabilities-cache
cache) and the whole transport U-series (`1edf806f`, `dbe5df73`, `6270b470`…).
So the curated matrix ran only **5** keys — `capabilities-cache` (added later)
was absent from `--keys` at that sha. Any re-run should dispatch off current
`main`.

---

## Scope assessment (task step 4) — are the highest-risk seams mutated?

The mutation surface is two disjoint target sets, both in
`scripts/`:

- **Scheduled sweep** — `scripts/mutmut_shortlist.py::DEFAULT_TARGETS`:
  `ops/submit_flow.py`, `ops/aggregate_flow.py`, `infra/transport/{__init__,_pull,_combiner}.py`.
- **Curated matrix** — `scripts/run_mutation.py::MODULE_MAP`: `block-chain`,
  `attestation`, `describe-cache`, `fast-path-cache`, `capabilities-cache`,
  `combiner`.

Verdict against the task's named high-risk seams:

| Seam | In scope? | Reality |
|---|---|---|
| `infra/transport/*` | **Yes** (scheduled) | but **0 mutants checked** this run — nominal scope, no signal |
| `ops/submit_flow` / `ops/aggregate_flow` | **Yes** (scheduled) | same — 0 checked; and **31/56** submit_flow fns are mutmut-BLIND behind lazy imports (shortlist) |
| `state/journal.py` | **NO** | exists; in neither target set |
| `state/index.py` | **NO** | exists; in neither target set |
| `ops/decision/*` | **NO** | exists (`decision_journal.py`, `ops/decision/`); not mutated |
| `_kernel/lifecycle/consent_hint.py` (consent seam) | **NO** | exists (`af7a39c4`); not mutated |
| `execution/mapreduce/combiner.py` | Yes (curated) | crashed → 0 results |

**Finding-2 (scope hole):** the journal / index / decision / consent seams —
the correctness-and-consent core where an undetected wrong-path is most
dangerous — are **entirely outside mutation scope.** The scheduled set covers
transport + the two flows (correct target choice) but the *state/decision*
substrate they ride is not mutated anywhere. Reduce-time/aggregate numbers
route through `combiner`, which is nominally curated but has never produced a
verdict (Finding 3).

Reachability tax (from `shortlist-report.txt`, informational): across the 5
scheduled files, **85 reachable / 49 BLIND**. `submit_flow` is the worst — 25
reachable, **31 blind** (canary decision, provenance, sidecar-match, array
submission all behind lazy `import`s mutmut cannot see). Even a *working*
scheduled sweep would mutate barely half of submit_flow.

---

## Raw results — per module

Verdicts from the uploaded `*.meta` `exit_code_by_key` (0 = survived,
1 = killed, 33 = no-tests, `null` = never executed) cross-checked against the
`mutation-results.txt` status listings.

### Curated matrix (`workflow_dispatch`)

| Module | Paired test file (the only `tests_dir`) | Total | Killed | Survived | No-tests | Status |
|---|---|---:|---:|---:|---:|---|
| `attestation` | `tests/state/test_attestation.py` | 83 | 71 | **12** | 0 | ran; survivors confounded* |
| `block_chain` | `tests/ops/test_block_chain.py` | 225 | 39 | **20** | 166 | ran; mostly artifacts* |
| `describe_cache` | `tests/cli/test_describe_cache.py` | 51 | 40 | **11** | 0 | ran; survivors confounded* |
| `combiner` | `tests/execution/mapreduce/test_combiner*.py` | 792 | — | — | — | **CRASHED — 0 verdicts** |
| `_fast_path_cache` | `tests/cli/test_fast_dispatch.py` | 117 | — | — | — | **CRASHED — 0 verdicts** |

\* "confounded" = the module has real test coverage in files OTHER than its
paired `tests_dir`, so a survivor here means "not killed by the one paired
file," NOT "not killed by the suite." Confirmed coverage-elsewhere for all
three: `attestation` (10+ files incl. `contracts/test_*_boundary.py`,
`ops/test_decision_journal_primitives.py`); `block_chain`
(`contracts/test_spec_hint_completeness.py`, `_kernel/lifecycle/test_block_drive.py`);
`describe_cache` (`tests/cli/test_describe.py`, `test_capabilities_cache.py`).

### Scheduled sweep (`sweep` job)

| Module | Mutants | Checked |
|---|---:|---:|
| `ops/submit_flow` | 2192 | **0** |
| `ops/aggregate_flow` | 1798 | **0** |
| `infra/transport/__init__` | 1024 | **0** |
| `infra/transport/_pull` | 863 | **0** |
| `infra/transport/_combiner` | 199 | **0** |
| **Total** | **6076** | **0 — all `not checked`** |

The `sweep` job wall-clock was <1 min. It rewrites `paths_to_mutate` but leaves
`tests_dir` at the **default (whole 8k-test suite)**, so `mutmut run` cannot run
even a single baseline+mutant inside the step and every mutant stays
`not checked`. This is not a fluke of this dispatch — it is structural: the
scheduled sweep has almost certainly produced zero verdicts on every weekly run
since B4 landed.

---

## Survivor triage

Because every curated module is coverage-confounded (single-file `tests_dir`),
**no survivor below can be certified a genuine suite gap without re-running the
module against its full covering test set.** Triage is therefore at the
behavior-class level, with the honest verdict recorded. (Per-mutant source
diffs were NOT in the artifact — the workflow uploads only `*.meta` exit codes,
not the mutated source — so exact operator-per-index is inferred from reading
the function + mutmut's deterministic scheme, not quoted.)

### block_chain (20 survived, 166 no-tests) — verdict: SCOPING ARTIFACT (near-total)

| Function | Survived / total | Verdict |
|---|---|---|
| `_wrap_run_id_under` | 14/14 | **SCOPING ARTIFACT** — killed by `contracts/test_spec_hint_completeness.py` (lines 155-157 test the exact flat-`run_id`→`{monitor:{run_id}}` reshape), excluded from `tests_dir`. Not a gap. |
| `_complete_spec_hint` | 3/4 | **SCOPING ARTIFACT** — same contract test routes every hint through it. |
| `_compose_submit_s2/s3/s4_spec`, `_compose_aggregate_run_spec`, `compose_successor_spec`, `successor_spec_sha`, `_spec_wall_clock_budget`, `verb_deadline_seconds`, `SuccessorSpecIncomplete.__init__` | 166 **no-tests** | **SCOPING ARTIFACT** — all exercised by `test_spec_hint_completeness.py` (compose round-trips, `SuccessorSpecIncomplete.missing`, sha tamper), excluded from `tests_dir`. |
| `chain_successor` | 2/16 | **CANDIDATE / likely EQUIVALENT** — `order[idx+1] if idx+1<len(order) else None`. Residual 2 are boundary/arithmetic mutants on the terminal-index guard; also cross-covered by `test_block_drive.py`. Low value; verify before building. |
| `next_block_hint` | 1/17 | **LOW-VALUE** — single residual in the hint-dict assembly; cross-covered. |

### attestation (12 survived) — verdict: CONFOUNDED; predominantly LOW-VALUE

`validate` 8/60, `bind` 4/12; `reduce` fully killed (0/11). `validate` is
almost entirely `raise errors.SpecInvalid(f"…")` guards; the survivors are
consistent with **error-message string-literal mutations** (tests assert *that*
it raises, not the message text) = **LOW-VALUE**. `bind`'s 4 residuals sit on
the recompute-mismatch message / the `callable(recompute)` branch. Because
`attestation` is exercised by 10+ other test files (all the boundary tests, the
journal primitives) that were excluded from `tests_dir`, treat all 12 as
**CONFOUNDED** — re-run against the full attestation covering set before
concluding anything. The load-bearing `bind` mismatch-refusal and `reduce`
drift-revocation were KILLED, which is the reassuring result.

### describe_cache (11 survived) — verdict: the ONE semi-trustworthy set; 1-2 genuine candidates

Still coverage-confounded (`test_describe.py`, `test_capabilities_cache.py`
excluded), but the closest to a real signal.

| Function | Survived / total | Behavior class | Verdict |
|---|---|---|---|
| `store` | 3/14 | The best-effort persist guards (`mkdir(parents=True, exist_ok=True)`, `atomic_write` inside `try/except OSError`) | **LOW-VALUE / EQUIVALENT** — swallowed-by-`except` mutations are undetectable by construction |
| `_full_registration_done` | 3/10 | `bool(getattr(primitive,"_REGISTRATION_DONE",False))` — the **A1 build-poison guard** (store must no-op under a partial registry) | **CANDIDATE TEST-GAP** — if the `default=False`→`True` or the attr-name mutant survived, no test asserts `store()` no-ops when `_REGISTRATION_DONE` is unset. Medium risk (a false-positive registry latch poisons every full-path `describe` reader for the build). Verify against full covering set. |
| `load` | 3/11 | `data if isinstance(data,dict) else None`; the `except (OSError,ValueError)` tuple | **CANDIDATE / LOW-VALUE** — a surviving `isinstance(data,dict)` mutant = non-dict cached payload not rejected. Low blast radius (self-healing to live-compute). |
| `_cache_path` | 2/9 | path-segment string literals (`"describe_cache"`, `f"{name}.json"`) | **LOW-VALUE** — on-disk path-name mutations, no behavioral assertion pins the literal dir name |

### combiner / fast-path-cache — NO DATA (infra crash)

- `combiner`: `mutmut` stats phase crashed —
  `FileNotFoundError: [Errno 2] No such file or directory: 'src'` inside
  `record_trampoline_hit → Config.source_paths[p].resolve(strict=True)`. Root
  cause: `test_combiner.py::TestMainEndToEnd::test_main_produces_wave_file` calls
  `main()` which **chdir's**, breaking mutmut's *relative* `source_paths`
  resolution. Zero verdicts on the highest-value curated module.
- `fast-path-cache`: `mutmut` aborted at baseline with
  `FAILED: Unable to force test failures` (21 passed, mutmut could not force a
  known failure to validate the harness). Zero verdicts.

---

## Ranked build units

Ranked by risk of the untested behavior. **The harness/scope units (1-4)
gate everything** — until they land, no future run can name a transport/journal
gap, so they outrank the individual test assertions.

### Unit A — Make the scheduled cluster-verb sweep actually check mutants  ★ highest
- **Why:** submit_flow / aggregate_flow / transport = the seams where a silent
  wrong-path burns real cluster time, and the sweep has checked **0** of 6076
  mutants (structural: default whole-suite `tests_dir`). The B4 scheduled job is
  currently a no-op.
- **Fix:** give the sweep a scoped `tests_dir` (mirror `run_mutation.py`'s
  per-module test selection, or a curated cluster-verb test set) so a baseline +
  mutants fit the step; consider chunking the 6076 across a matrix like the
  curated job. Optionally fail the job (not the merge) if `checked == 0` so a
  zero-signal run is never green-and-silent again.
- **Files:** `.github/workflows/mutation.yml` (the `sweep` job),
  `scripts/mutmut_shortlist.py` (add a `tests_dir` selection lever).
- **Size:** M (workflow + one script lever + a "checked>0" tripwire).

### Unit B — Fix the two curated-module crashes  ★ high
- **combiner:** the `test_main_produces_wave_file` chdir breaks mutmut's
  relative `source_paths.resolve(strict=True)`. Fix by making the test restore
  cwd (tmp_path + `monkeypatch.chdir` teardown) OR configure mutmut with an
  absolute `source_paths`. Recovers the highest-value curated target (792
  mutants).
- **fast-path-cache:** `Unable to force test failures` — mutmut's baseline
  needs the paired tests to be able to fail on a forced mutant; investigate
  whether `test_fast_dispatch.py` has an all-pass-regardless path.
- **Files:** `tests/execution/mapreduce/test_combiner.py`,
  `tests/cli/test_fast_dispatch.py`, possibly `scripts/run_mutation.py`
  (absolute `source_paths`).
- **Size:** S each.

### Unit C — Broaden each curated module's `tests_dir` to its true covering set  ★ high
- **Why:** the single-file pairing manufactures false survivors (proven:
  block_chain's 183 survivors/no-tests are all killed by an out-of-scope
  contract test). Every curated survivor list is currently untrustworthy.
- **Fix:** `MODULE_MAP[key].tests` should list ALL files that cover the module
  (e.g. block-chain += `tests/contracts/test_spec_hint_completeness.py`,
  `tests/_kernel/lifecycle/test_block_drive.py`; attestation += the boundary
  tests; describe-cache += `tests/cli/test_describe.py`). Accept the longer
  per-module runtime — a false survivor costs more than the CI minutes.
- **Files:** `scripts/run_mutation.py` (`MODULE_MAP`).
- **Size:** S (data edit) + a re-dispatch to reap the true survivor set.

### Unit D — Add the correctness/consent/journal seams to mutation scope  ★ high
- **Why:** `state/journal.py`, `state/index.py`, `ops/decision/*`,
  `_kernel/lifecycle/consent_hint.py` are in NO target set. These rank above
  renders per the risk ordering and are unmutated today.
- **Fix:** add them as curated `MODULE_MAP` entries (each with its true covering
  test set per Unit C) — pure-logic, high-value, exactly the runner's selection
  criteria.
- **Files:** `scripts/run_mutation.py`.
- **Size:** S-M (needs one focused-test survey per module).

### Unit E — describe_cache: assert the A1 partial-registry store guard  (genuine, small)
- **Gap:** no test pins that `store()` **no-ops when `_REGISTRATION_DONE` is
  unset** (the build-poison guard `_full_registration_done` had 3/10 survive).
- **Assertion:** with `primitive._REGISTRATION_DONE` absent/False and a
  content-keyable build, `store(name, data)` writes no file; with it True, it
  does. (Also pin `load` rejects a non-dict payload.)
- **Files:** `tests/cli/test_describe_cache.py`.
- **Size:** S. Build only AFTER Unit C confirms these survive the full covering set.

### Not building
- block_chain `chain_successor` (2), `next_block_hint` (1) — scoping artifacts /
  likely equivalent; cross-covered.
- attestation `validate`/`bind` survivors — LOW-VALUE (error-message string
  mutations) + confounded; the load-bearing `bind`/`reduce` invariants were killed.
- describe_cache `store` swallowed-`except` and `_cache_path` path-literal
  mutations — EQUIVALENT / LOW-VALUE.

---

## Drift log

- **2026-07-17** — first triage of the B4 mutation runner (landed `a0dd47dd`,
  never triaged). Run `29560911639` @ `e41f25e2`. Headline: scheduled sweep
  checked 0/6076; 2/5 curated modules crashed; the 3 that ran are
  coverage-confounded by single-file `tests_dir`. No high-confidence suite gap
  produced. Docket = harness Units A-D + one genuine describe_cache assertion
  (Unit E). Recommend re-dispatch off current `main` AFTER Units A-C so a future
  run yields citable survivors. Artifacts (metas + results.txt) were downloaded
  to a scratch dir, not committed; the workflow retains them 30 days.
- **Open question for the operator:** should the scheduled sweep be *paused*
  until Unit A lands? It currently burns a weekly Linux runner for zero signal
  and reads green. Recommend either fix-forward (Unit A) or disable the
  `schedule:` trigger until then.
