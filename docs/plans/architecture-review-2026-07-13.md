# Architecture review — 2026-07-13 (banked proposals)

Status: **BANKED — review complete, no proposals executed yet.** Produced by a
15-reader whole-repo ingest (~370k lines), adversarially verified by 5 independent
review agents, then a completeness-critic + deep-test + mechanical-sweep second
round (corrections recorded in §F of the addendum). Paths and counts were
verified against the tree as of f3594d9; verify live before acting — this doc
narrates a plan and is deliberately outside the d-pins truth surfaces.


**Method.** 15 reader agents ingested the full repo (~370k lines: all of `src/`, tests structurally,
all docs — excluding `docs/generated/` and lockfiles). Findings were synthesized against the repo's
own constitution (`docs/internals/engineering-principles.md`, `docs/architecture.md`), and the five
highest-stakes proposal clusters were adversarially verified by independent Opus agents that read the
cited code before ruling CONFIRMED / WEAK / REFUTED. Refuted claims were dropped; corrections are
folded in below.

---

## Overall verdict

This is an unusually intentional codebase. The registry-as-single-source-of-truth
(~169 `@primitive` verbs — verify live, per the repo's own rule — projecting CLI, MCP, JSON schemas, and docs), the one-attestation-kernel trust
substrate, escalation-as-data, detach-by-contract, positive-evidence transport, and the enforcement-map
discipline (every mechanizable rule held by a lint/test with a demonstrated fire path) are all working
as designed. The problems are not design failures. They are three systematic patterns:

1. **Accretion at lint-forced seats.** The one-definition doctrine + subject-isolation lint funnel
   everything to single files, which then grow without bound (`journal.py` 3.8k lines, `submit_flow.py`
   3.0k, `transport.py` 2.2k, `mcp_server.py` 2.1k, `status.py` 1.4k — regrown past its own extraction
   precedent).
2. **Enforcement blind spots in directions the lints don't scan.** `lint_subject_imports.py` scans only
   `ops/`/`meta/`, so `infra→ops` and `incorporation→ops/meta` inversions — including one genuine
   bidirectional import cycle — are structurally invisible. One CI gate cannot fire where CI runs it
   (`lint_plugin_manifests`), the template-boundary test's scoped scan hides three live scaffold
   violations, and `regen-pr`'s self-heal
   mechanism advertises something GitHub tokens cannot do.
3. **Prose drift the repo already predicted.** The drift log's thesis ("prose rots, mechanized checks
   don't") is confirmed everywhere: `docs/architecture.md` still lists deleted worker surfaces, four
   docs describe a deleted `hpc-campaign-driver`, two SKILL files reference slash commands that never
   existed, `incorporation/README.md` documents a nonexistent directory, and `engineering-principles.md`
   itself has drifted (its ship-list example) and now exceeds the 25k-token read cap of the repo's own
   agents.

---

## Ranked proposals

### P1 — Close the layering blind spots (VERIFIED; highest value-to-risk)

| Fix | Verdict | Detail |
|---|---|---|
| Move `ops/transfer/{manifest,prune}.py` → `infra/` | CONFIRMED | `infra/transport.py` lazily imports them at 7 sites; manifest is pure stdlib, prune depends only on manifest, the only non-test importer is transport itself. No cycle created. |
| Break the `incorporation ↔ ops/submit_flow` cycle | CONFIRMED (strongest) | `build/submit_spec.py:835,1214` imports private `_GENERATED_SHIPPABLE`/`_is_bare_script_name` from `ops/submit_flow`, which itself imports `check_per_task_executor` back (:1030). Both lazy — one promotion to module scope breaks import. Move the executor-shape predicates to a lower shared home; this also restores `incorporation/README.md`'s stated (currently false) invariant. |
| Make `_campaign_spec_identity` public in `meta.campaign` | CONFIRMED, fix corrected | `ops/overnight.py:1394` reaches a private symbol. ops→meta.campaign is an established public pattern; the defect is only the underscore. Do NOT relocate to `infra/` (it carries campaign-domain identity fields). |
| Extend the lint | — | Add `infra→ops` and `incorporation→{ops,meta}` to the forbidden directions (new lint or extend `lint_subject_imports.py`); today these directions are structurally unscanned. Include the standard synthetic fire-path test. |
| Leave `state/run_story.py → ops.overnight` alone | WEAK (sanctioned) | The lazy import is documented in `docs/design/run-story.md` as the intended one-definition mechanism; the rule bars only *eager* state→ops edges. Relocating the reader would split it from its writer. |
| Extend the template-boundary test to `scaffolds/` | CONFIRMED (reader + domain agent) | 3 of 7 strategy scaffolds import `reduce.history` (not on the allowlist, pulls `state.runs`); `test_templates_do_not_import_core` scans only `templates/runtime/`. Doc, test, and code disagree — either allowlist scaffolds explicitly with a recorded rationale, or fix the imports. |

### P2 — Split the god seats in place (VERIFIED for the flagship; pattern generalizes)

The mechanism: convert module → package at the same import path, `__init__.py` re-exports every
current symbol, and **move the test pins in the same commit** (the repo's own re-point-first doctrine).

- **`ops/decision/journal.py` (3,801 lines, 12 authorship gates)** → `ops/decision/journal/<gate>.py`.
  CONFIRMED: the subject-import lint is a non-issue (intra-subject nesting is legal; `_subject_of` is
  the first path segment). One real hazard found by verification: **three tests do whole-module
  `inspect.getsource(journal)` scans** (`test_utterance_route_through.py:100`,
  `test_multi_human_boundary.py:134,250`); on a package these silently return only `__init__.py` and go
  vacuous while staying green. Re-point them to concatenated submodule sources in the same commit —
  mandatory, not optional. Payoff: the repo's #1 hot file (serialized across worktrees by standing rule)
  stops being a merge bottleneck.
- **`incorporation/build/submit_spec.py` (1,345 ln)** — the ~950-line executor-guard library (whose
  entry `check_per_task_executor` is already consumed independently by 4 ops modules) wants its own
  module; combined with the P1 predicate move this cleans the whole seam.
- **`_kernel/extension/mcp_server.py` (2,054 ln)** — the elicitation/render-digest half (~700 ln) is a
  separable module; the JSON-RPC pump, registry projection, and policy remain.
- **`_kernel/hooks/relay_audit_stop.py` (1,272 ln, five audits)** — subpackage, but **keep the hook
  entry module path**: `agent_assets` writes `hpc_agent._kernel.hooks.<module>` needles into users'
  `~/.claude/settings.json`; renaming the entry module orphans installed hooks.
- **`agent_assets.py`** — four ~90-line copies of the same read/skip-unparseable/dry-run/atomic-write
  merge skeleton (plus 9 hook-entry builders) collapse into one generic merge core (~400 lines saved).
- **`infra/transport.py` (2,219 ln)** and **`execution/mapreduce/reduce/status.py` (1,431 ln)** — same
  treatment; status.py's ~240-line scheduler-profile resolver is `infra/backends`-shaped, not
  reduce-phase-shaped, and status.py has regrown past its own recorded extraction precedent (rollup.py
  was extracted when the file hit 752 lines).

### P3 — CI/enforcement repairs (VERIFIED; small, high leverage)

- **`regen-pr` cannot do what its header claims** (CONFIRMED): pushes with `GITHUB_TOKEN` don't
  trigger workflows, and the `test` job checks out the immutable pre-regen SHA. Adopt docs.yml's
  proven pattern: regen `--write` in the same job, before the `--check` gates.
- **`lint_plugin_manifests` can never fire** (CONFIRMED): the `test` job runs it with zero plugins
  installed (no-op exit 0); the `plugins` job installs plugins but never runs it. Move the invocation
  into the `plugins` job. This is the repo's own "verify a guard can actually fire" class.
- **Lints + mypy run 3× across the Python matrix** (CONFIRMED): hoist to one leg; saves 2 redundant
  runs per PR.
- **ci.yml/docs.yml auto-commit race** (CONFIRMED, mild): both regen the same two doc files on the
  same PR event; worst case is a spurious failed push, but one owner is cleaner.
- **Add fire-path tests to the boundary scanners that lack them** (`test_challenge_boundary`,
  `test_conformance_boundary`, `test_evidence_boundary` banned-word scans pass on clean input but never
  demonstrate they fire — the suite's own discipline, applied inconsistently).
- **SGE integration CI**: the container CI covers SLURM only while the primary target (Hoffman2) is
  SGE, and the G9 generator confirmed whole SGE recovery paths shipped broken. Either build the SGE
  container or record the decision not to.
- Refuted, for the record: primitive-index drift on direct pushes IS caught (docs.yml gates on push);
  the `.claude/settings.json` `rm -r :*` deny rule DOES match.

### P4 — Consolidations, corrected by verification (each small; respect re-point-first)

- **`select_window`**: divergence CONFIRMED (inclusive-datetime vs exclusive-lexicographic under one
  name) but latent, not live — the `conformance.py` variant has zero production callers. Delete/alias
  the unused one; unify the three byte-identical canonical-sha spellings
  (`conformance.canonical_content_sha` / `conformance_store._canonical_observation_sha` /
  `determinism.canonical_sha`) through the one kernel.
- **Canonical-sha copies in ops**: fold `audit_view`, `prereqs`, `verify_op` through
  `determinism.canonical_sha` (all `ensure_ascii=False`, byte-identical; `prereqs`'s "no helper of this
  exact form exists" comment is demonstrably stale — it already imports from determinism). Do NOT touch
  `provenance_manifest.manifest_signature`: it uses the default `ensure_ascii=True` byte form and the
  data is already signed; migrating requires a version bump.
- **`FailureCategory` twins**: do NOT derive one Literal from the other — resubmittability is a
  genuinely separate axis (`failure_signatures.py:449` reasons about a future member belonging to
  neither). Compose both from a shared member tuple (or add an equal-today drift test) so additions
  can't silently desync — the mode that already bit twice.
- **interview.json readers**: not one family of six but two families of three, plus a seventh canonical
  copy already in `state.pack_declarations`. Extract only the tolerant locate+load skeleton to `state/`
  (subject-neutral — the subject lint forbids an ops-side shared home); each subject keeps its own
  block extraction.
- **Detach-by-contract**: extraction largely REFUTED — the mechanism is already centralized in
  `_kernel/lifecycle/detached.py` and the per-site replay/record variation is semantic. What survives:
  collapse the 5× byte-identical `_*_cmd_sha` helper into `state/`, and add a shared
  `DetachedHandleFields` base for the 6× re-declared wire triple.
- **Unverified but multiply-attested quick wins**: preflight composite commons (~90-150 byte-similar
  lines ×4: `SubCall`/`_synth_error_subresult`/`_run_subprocess`); route `infra/inspect/_persist.py`
  through `infra.io` atomic writers (io.py's docstring already claims it as a caller — stale);
  the schema-resolution ladder duplicated "in lockstep" between `contract/schema.py` and
  `registry/operations.py`; test-suite fixture extraction (fake-optuna ~130 ln ×2, `run_cli` wrapper ×3,
  `journal_home` ×4 in tests/meta, local conftests for the 7 directories that have none).

### P5 — Contract hygiene: make de-facto public APIs honest

- **Promote-to-public sweep**: dozens of underscore-private symbols are imported across module and
  package boundaries (`reconcile._sibling_run_ids`, `run_record._current_homedir` from 4+ packages,
  `remote._capture_via_select` from kernel, `detached._pid_alive` from 5 ops files, ...). The repo's
  "re-point, never duplicate" intent is right; the naming denies the contract. Rename the genuinely
  shared ones public (or move to the owning module's `__all__`) and add a lint to hold the line.
- **Declare the plugin API**: the notebook-render example plugin imports `ops.notebook.*`, `state.*`,
  `cli._dispatch`, `_kernel.registry` — the de-facto plugin surface is far wider than the documented
  seam (`infra.backends` + `plugin_manifest`). Any reorg breaks a shipped, CI-gated plugin. Either
  version the wider surface or narrow the plugin.
- **`_wire` suffix lint**: the `*Spec`/`*Input` suffix convention is load-bearing for schema emission —
  a third suffix silently emits nothing, and `schema_for()` degrades to `None` on rename rather than
  failing loudly. A small lint (every public wire model resolves to an emitted schema or is an
  explicit helper) converts two silent failure modes into loud ones.
- **`_wire/fixtures/` mis-shelving**: it holds the most load-bearing cross-cutting shapes (envelope,
  escalation, failure-features), while persisted-record schemas (receipts, DecisionRecord) scatter
  through verb modules. A rename/regrouping is possible but expensive (schema discovery keys on
  `__module__`); at minimum document the layout, at best move with a regen commit.

### P6 — Docs truth pass (cheap; the entry points are lying)

1. `docs/architecture.md` — remove the deleted worker surfaces (worker_prompts/, invoke.py,
   llm_resolver) from the diagram; it's the most-read entry point and currently misstates the surface
   inventory.
2. Purge the `hpc-campaign-driver` ghost from 4 docs (workflows/README, campaign.md, state-model.md,
   CONTRACT.md — the last also contradicts code-driven-orchestration.md on the deleted resolver seam).
3. Fix or create the referenced-but-nonexistent slash commands (`/classify-axis-hpc`,
   `/wrap-entry-point-hpc` — referenced by two SKILL files and a prompt builder; no file, no history).
4. Split `engineering-principles.md`: keep the normative enforcement maps; move per-incident narrative
   to the dockets it duplicates. At ~31-36k tokens it exceeds a 25k-token single-read cap in common agent tooling (the operative point: it can no longer be read in one pass).
5. Regenerate `docs/README.md`'s navigation map (it omits `design/` entirely) — or generate it, like
   everything else here.
6. Separate historical audit logs from normative contracts inside `docs/internals/` (the R4 pattern
   already applied to design/); `bug-sweep-2026-07-11.md` still says "no fixes applied yet" though the
   fix train shipped.
7. Retire the 11 legacy `inputs:` frontmatter blocks the generator round-trips forever (declared
   obsolete by the catalog's own README; zero CI validation against schemas).
8. One regen-debt ledger (outstanding "rebake at merge" notes are scattered across 5 drift logs).

### Also surfaced (not architecture, but confirmed en route)

- `checkpoint.py::run_iterations` fresh-vs-resume discriminator is an **open MEDIUM bug**
  (bug-sweep #30, unrefuted): tests `state is None` where `resume_point <= 0` is correct.
- Dead-code pass candidates (apply verify-a-guard-can-fire before deleting): `plugin_worker_prompt_roots`
  (consumer deleted), `errors._registry_remediation`, evidence_brief_op's landed-T4 fallback,
  `prepare_phase2_spec` (self-declared non-production), `status_preflight` doc/decorator contradiction,
  stale docstrings across `_kernel` referencing deleted modules.

---

## What NOT to do (the repo already ruled on these)

- Don't move workflow files back into subject dirs, re-introduce a permissive import allow-list, or
  inline cross-subject composition (architecture.md non-goals, P5a deliberate).
- Don't collapse the standalone-boundary lock-step duplications (dispatch/combiner/announce constants,
  `_CHECKPOINT_RES`, inline `Flag`) — they're pinned by parity tests; the deploy closure can't import
  the package.
- Don't add sticky-terminal guards, unlock/cancel/browse verbs, or LLM calls in render paths.
- Don't unify `state/run_sha.py`'s bytes with `canonical_sha` (deliberate, journal keys) or fold
  `story_render` into `relay_render` (recorded non-unifications).
- Any move of a one-definition symbol must carry its `inspect.getsource` pins in the same commit —
  the brittleness is the mechanism, not an accident.

---

# Addendum — Round 2 (completeness critic + deep test passes + mechanical sweeps)

After the first delivery, a Fable completeness critic audited the review against all 15 reader
reports, two Sonnet agents deep-read the ~300 test files the structural skim had sampled past, and
exhaustive mechanical sweeps (full import graph, mirror-comment ledger, generated-artifact orphan
check, git churn, TODO census) were run over the whole tree. This addendum records what changed.

## A. Upgraded finding: the full inversion census (mechanical, exhaustive)

The AST sweep of every import in `src/` (eager and lazy) settles the anecdotes:

| Direction | eager | lazy | Note |
|---|---|---|---|
| ops → cli | **110** | 7 | the documented CliShape "declaration vocabulary" |
| meta → cli | 13 | 1 | same vocabulary |
| _kernel → cli | 7 | 5 | registry/schema/hooks |
| _kernel → ops | 2 | 11 | block_drive, mcp_server, hooks |
| infra → ops | 0 | 7 | transport → ops.transfer (P1) |
| infra → state | 2 | 5 | mutual entanglement |
| incorporation → ops/meta | 0 | 5 | the P1 cycle |
| state → ops/meta/cli | 1 | 2 | one sanctioned + discover.py CLI verb |
| _wire → infra | 0 | 1 | documented lazy backend-registry validation |

Consequence: **moving `CliShape`/`CliArg`/`SchemaRef` out of `cli/` into `_kernel/contract/` is
promoted from "optional renegotiation" to a first-class proposal.** The docstring calls the inversion
intentional, but 130 eager import sites means a tenth of the codebase's import graph rests on a
vocabulary that lives in a surface package. Moving the three dataclasses (with a `cli._dispatch`
re-export shim for compatibility) makes the documented intent structural and cuts the largest
inversion in the census to zero. Small mechanical risk; the shim preserves every existing import.

## B. New proposals (from the critic — the dropped synthesis themes, restored)

- **N1. In-process invocation seam for the preflight composites** (the dropped performance lens).
  Every agent-facing preflight pays 2-4 × ~1.2s CLI subprocess cold-starts at the latency-critical
  surface, while `_in_process_cli_runner` (mcp_server.py:882, ~40ms) is already proven twice over
  (MCP + the contracts suite's `invoke_cli`). Extract the runner to a neutral home (it living in the
  MCP extension would give ops→kernel.extension an odd edge), route the composites through it, keep
  the subprocess path as the parity oracle. Riders from the deep test pass: preserve the #277/#289
  fan-out independence invariant and the post-#295 "runtime_uv + ssh_echo share exactly ONE ssh_run
  connection" pin; consolidate the two wall-clock (`elapsed < 0.7s`) Barrier-based concurrency proofs
  into one robust helper while there.
- **N2. Govern the ops role root** (the dropped structural remedy for pattern #1). ~30-35 files sit
  at the lint-exempt role root, so most cross-subject traffic flows through unscanned files. The docs
  non-goal forbids moving workflows back into subject dirs — it does not forbid *linting the root*:
  extend `lint_subject_imports.py` with a role-root pass that checks each root file's cross-subject
  imports against its own `composes=` declaration (the registry already carries the data), allowlist +
  synthetic fire-path test included. Converts the exemption from "unscanned" to
  "scanned-with-declared-edges" without renegotiating any recorded decision. Highest-leverage new
  proposal: it addresses the root cause P2 only treats symptomatically.
- **N3. Scan the kernel direction with an explicit allowlist.** P1's lint row hardened `infra→ops`
  and `incorporation→*` but left `_kernel→ops` (13 sites) and `state→ops` unscanned — the same
  blind-spot class. kernel→ops is partly inherent (the drive loop routes verbs) and the CliShape
  inversion is documented, so the right mechanism is direction-scanning with enumerated sanctioned
  exceptions, not edge-fixing.
- **N4. Non-creating layout accessors** (root cause of a P4-class duplication family). `RepoLayout`
  mkdirs on property access (layout.py:56-69), which forces every non-creating reader to hand-build
  paths — producing the scope-kind→journal-path map ×3 (decision_journal / evidence / challenges,
  verified mirrors), `evidence._sample_admitted` re-implementing `fingerprint_store._is_admitted`,
  and inline scope-lock re-derivation. Add non-creating twins with their own "never mkdirs" fire-path
  contract test, then collapse the three maps to one helper and re-point.
- **N5. Shared stateful SSH fake for the test suite.** The suite's dominant seam
  (`hpc_agent.infra.remote.ssh_run`, 23+ direct patch sites plus dozens of aliases, plus a parallel
  5× `rsync_pull` stub family and 3× booby-trap variants) is faked by hand-rolled per-file closures.
  Working in-repo templates exist: `tests/_mcp_harness.py::FakeMcpClient` (stateful duplex,
  cross-suite reused), `test_watcher_install.py::_ScriptedSSH` (substring-keyed rule list),
  `_FakeSchtasks`/`_FakeCrontab` (stateful). A `tests/_ssh_fakes.py` with ack-sentinel-aware builders
  (the `__HPC_*_ACK__` convention is already uniform) adopts incrementally, no flag day.
- **N6. Deprecation-expiry ledger.** The tree carries many time-boxed compat shims with no expiry
  mechanism (`legacy_terminal_block_keys` "remove once no mid-flight run predates the fix", the
  37-name `_MOVED` root shim, Item-6 "for one release" re-exports, `HPC_HOMEDIR` monkeypatch hack
  "kept for ~20 legacy test files", deprecated forwarders in state/). Hold them in the repo's own
  strict-xfail punch-list idiom (`test_recovery_registry.py` precedent) so an outlived shim fails
  loudly. Pairs with P6.8's regen-debt ledger.
- **N7. Mirror-ledger lint.** The mechanical sweep found **84 files** carrying "kept in
  sync/lock-step/mirrors X" comments — far more than any reader enumerated. Most are the sanctioned
  standalone-boundary carve-out; nobody has verified all 84 have pinning tests. A lint requiring each
  mirror comment to name its twin + its pinning test converts the convention into a checked contract.
- **N8. Schema-reachability lint.** The orphan sweep: 169 ops = 169 docs (zero drift — the gates
  work), but 16 of 244 schema stems match no op name. Most are explainable (shared block outputs,
  persisted-file schemas, `schema_for()` fallback tiers, worker-compat) — but `evidence_demand` has
  no obvious owner, and the resolution ladder silently degrades to `None` on rename. A lint asserting
  every schema file is reachable from `schema_for()` or an explicit allowlist closes the loop (same
  family as the _wire suffix lint in P5, which — clarified — targets src model class names, not test
  filenames).

## C. Sharpened proposals (deep-test-pass riders on P2)

- **journal.py split, staged path**: `append_decision` is one ~3,400-line dispatcher, and three
  private helpers (`_fresh_human_texts`, `_newest_lock_ts`, `_target_record_ts`) are shared state
  across 4 gates — hoist them into ONE shared internal module, never per-submodule copies. Formalize
  `scope_lock.py`/`verify_relay.py` as package members first (no private-import coupling); keep
  `journal.py`/`__init__` as a thin dispatching facade. Treat the two live entry points
  (`state.decision_journal.append_decision` facade vs the ops primitive) as one canonical entry.
  One more source-text pin to carry: `test_decision_journal_primitives.py:1320-1341` pins
  `_assert_signoff_authorship`'s source to contain `attestation.bind(`/`build_canonical_view(`.
- **submit_flow split riders**: ~19 private symbols are pinned by tests (one imported at module
  level); `_run_shared_prelude` is pinned independently by the notebook-gate AND pack-gate test
  files; `test_toy_pack_integration.py` reads the literal source text of `submit_flow.py` **off
  disk** and asserts an exact call string — it hardcodes the monolith's file path and must be
  re-pointed. `submit_and_verify` patches `sav.submit_flow` by attribute — keep that re-export.
  Encouraging precedent: `ops/submit/` is already a package and 14 of 26 submit test files have zero
  coupling to `submit_flow.py` internals. Caution: the already-split `ops/submit/runner.py` exhibits
  the same private-import anti-pattern internally (`_DEDUP`/`_PROCEED`/`_REFUSE`/`_resolve_layer1`
  imported by tests) — **the promote-to-public sweep is a prerequisite of the splits, not a parallel
  nice-to-have**, and must cover new submodules too.
- **P2 mandatory rider (critic-verified)**: `scheduler-integration.yml:26-29` path-triggers name
  `submit_flow.py`, `monitor_flow.py`, `aggregate_flow.py`, and `transport.py` as exact file paths.
  Any module→package conversion of those files must update the `paths:` globs in the same commit or
  the only real-scheduler CI silently stops covering the code it exists to guard.
- **Preflight commons, descoped**: `ops/preflight/` (SSH-probe layer) and the `*_preflight.py`
  CLI-orchestration modules are architecturally disjoint — a naming collision, not shared logic. The
  commons extraction targets only the orchestration layer's `SubCall`/`_synth_error_subresult`/
  `_run_subprocess` triplication (+ N1). Separately fix the intra-directory duplication in
  `tests/ops/preflight/` — including a `green_local_env` fixture-name collision with two
  incompatible signatures.
- **promote-to-public sweep, sized**: the deep pass catalogued ~20 modules whose privates are
  reached across boundaries; highest-leverage targets by blast radius are
  `state.run_record._current_homedir` (5 independent test files, 4 subdirectories) and
  `mcp_server.py` (~18 distinct private symbols reached by tests — the largest single concentration).

## D. Adjudications (where round-2 evidence met the Opus verdicts)

- **ops/transfer → infra move: STRENGTHENED.** 8 lazy import sites (one more than the verifier
  counted); transport is the only production consumer; the infra tests already treat transfer as an
  infra concern. One dissenting sub-agent read the lazy import as an intentional boundary — rejected:
  the laziness is documented as import-cost hygiene, the pure-planner/wiring test split survives the
  move unchanged (modules and their tests move intact), and no doc records the direction as intended.
- **Canonical-sha unification: scope clarified.** The state-side P-S1 unification is already done and
  byte-pinned by tests (manifest_doc_sha, records_sha, content_sha_over_payloads). What remains is
  exactly the Opus-verified ops-side fold (audit_view, prereqs, verify_op → `determinism.canonical_sha`,
  with a byte-pin test; `provenance_manifest` excluded — different byte form, signed data). One
  provenance caveat: the "prereqs already imports from determinism" line came from the verifier and
  was not independently re-verified; check before acting.
- **interview.json loader: kept, modest.** The test-level consolidation angle is empty (only two
  trivial call sites), but the source-level finding stands as verified: two families of three readers
  plus a seventh canonical copy in `state.pack_declarations` — extract only the tolerant locate+load
  skeleton to state/.
- **journal_home fixture duplication: ×18, not ×4** (12 in tests/meta, 6 in tests/state/cli) — but
  the root conftest's autouse `_isolated_journal_home` already isolates; the copies exist only to
  return the path, so the fix is one returning fixture in the root conftest. Plus newly catalogued
  families: `_seed_iteration` ×7 in tests/meta (~250 lines), `_redirect_home` ×4, `run_cli` is
  actually ×8, `_FORBIDDEN_FIELD_NAMES` ×4 more in tests/_wire (one copy deliberately drops
  "baseline" — parameterize, don't flatten), matchers quartet with identical 16-line docstrings,
  `FakeClock` ×2, `_noop_ssh` ×8 in tests/infra/backends, banned-word render check implemented 4
  different ways.
- **"No stateful fakes" corrected**: true for the SSH seam specifically; the suite has excellent
  stateful fakes elsewhere (FakeMcpClient, _FakeSchtasks/_FakeCrontab) — reframed as N5's templates.

## E. New "Also surfaced" items (verified)

1. **Live functional defect, promoted to the top of the list**: `ops/registration/prereqs.py:553-567`
   still unconditionally refuses the `pack-receipt` prerequisite kind as "not landed" — but
   `state/pack_receipts.py` and the whole shipped `ops/pack/` family exist. A registration chain
   declaring a pack-receipt prerequisite is unusable and the remediation text is false. Fix: wire
   `_check_pack_receipt` through `state.pack_receipts` the way `pack_gate.assert_pack_receipts_current`
   already does. (A reader found this; the first synthesis dropped it.)
2. **Integrator-facing contract docs truth leg (P6 item 9)**: CONTRACT.md's "no cancel primitive —
   permanent design choice" vs the shipped `kill` primitive (verified contradiction in the doc external
   harnesses build against; needs re-scoping, not just deletion); sync-checklist's "17 error codes" vs
   18 in errors.py; CONTRACT.md's `timeout`-as-terminal conflation of two enums; cli-spec.md's
   self-declared hand-maintained CLI↔primitive table and rotting "73 schemas" literal — generate it,
   like its five sibling artifacts.
3. **Committed workspace debris**: `.claude/fix-agent-diff-backups/` — four diff/status pairs from a
   2026-07-08 fix-agent run (two zero-byte) checked into git. Delete + gitignore.
4. **Windows CI decision**: win32 correctness code is load-bearing (named-pipe multiplexing,
   detached-process flags, advisory_flock — "the one correctness dependency" of multi-cluster) while
   `test-windows` is continue-on-error signal-only. Promote a minimal blocking leg or record the
   signal-only tier as a decision (the repo's own idiom, applied to SGE but not here).
5. **Philosophy-audit residue triage**: the 2026-07-12 audit's open drift items (B1 double 11k-token
   payload, B7 sixth read_terminal consumer skipping currency, B10 silent challenge drop "live at
   HEAD", B14 forensic tier still primary) were carried by no proposal; B4 has since landed, proving
   the list goes stale fast — half a day of verify-and-triage yields ready-made work items.
6. `ops/smoke_test_executor.py` default `output_file="/tmp/smoke.csv"` — fixed world-writable path
   on shared login nodes (symlink/collision hygiene).
7. Additional split candidates the size sweep surfaced: `ops/decision/verify_relay.py` (1,649) and
   `ops/verify_reproduction.py` (1,347); `dispatch.py::main` is a 662-line single function
   (deploy-constrained to one file, not to one function).
8. `_CURATED_EXTRA_VERBS` (mcp_server.py:156-268): 110-line hand-annotated allowlist, one entry for
   a verb that doesn't exist; only the skill-named subset is lint-policed. `recovery/`: 5 of 9
   RecoveryKinds unported with no schedule. Seven TTL-cache modules re-implement one skeleton with
   drifting env-flag idioms.
9. Hygiene positives from the sweeps, for calibration: only 11 TODO/FIXME markers in all of src/;
   churn since the fork is diffuse (max 5 commits/file) — the hot-file problem is size + serialization
   rules, not commit frequency; the 169=169 ops↔docs sync is perfect; the tests/ops tree at ~230
   files contains essentially zero dead test code.

## F. Corrections to round 1 (own-goal ledger, per the repo's spirit)

- "167 verbs" → **169** (frozen-literal sin the repo explicitly warns about).
- engineering-principles.md is ~31-36k tokens, not ~50k; the exceeds-one-read point stands.
- "Two CI gates cannot fire" over-classified the template-boundary test: it fires fine on its declared
  scope — the defect is a coverage gap concealing three live violations, which changes the fix.
- "Top-percentile codebase" was an editorial judgment presented as a method conclusion; softened.
- P2 originally omitted the scheduler-integration `paths:` rider its own load-bearing list implied.
