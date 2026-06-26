# Engineering principles

Cross-cutting judgment rules for maintainers (human or agent). This page is
**descriptive**: wherever a principle here is mechanizable, the normative copy
is the lint or test linked next to it, and CI — not this prose — is what holds
the line. The prose exists for the parts a linter cannot decide, and to record
*why* the enforcement looks the way it does.

This page replaced the repo's prose `CLAUDE.md` (now a one-line pointer
here). The deciding incident: of the three "current facts" that file
asserted, two had silently rotted (see the drift log below) while every
mechanized check stayed true. Lessons that can fire live in CI; only the
irreducible judgment calls stay prose.

## Verify a guard can actually fire before classifying it as "intentional"

When you hit a constraint, a defensive default, an apparent duplication, or
anything that *looks* deliberate, do not default to "leave it, it's by
design." Establish **which** it is: check whether the protection can actually
fire, and whether changing it alters behavior a real path or a test would
notice. A guard that can never fire is inertia, not design — and a comment
asserting a reason ("so legacy X validates", "cluster-side baseline") is a
claim to verify, not evidence.

This cuts both ways — apply it before you *preserve* something **and** before
you *remove* it. Case history:

- **Looked intentional, was inert.** Output schemas typed `run_id` as a loose
  `str` "so legacy sidecars validate." But `run_sidecar_path` already
  validates every run_id against the strict `^[A-Za-z0-9._\-]+$` pattern at
  the filesystem layer, so the loose-output guard could never accept anything
  the strict one wouldn't — and the one case it *could* fire (the framework
  emitting a malformed id) is a bug it would hide rather than catch. Tightened
  to `RunIdStrict` on output.
- **Looked intentional, was misattributed.** `infra/parsing.py` was assumed to
  be a "cluster-side baseline" that couldn't import the package. Verified
  false: `deploy_runtime` ships only what `transport._build_deploy_items`
  enumerates — `dispatch.py`, `combiner.py`, `metrics_io.py`,
  `executor_cli.py`, and the rendered shell templates plus preambles — and
  every importer of `parsing.py` is control-plane. The module's stdlib-only
  rule stands on its own merits; its docstring now says so.
- **Looked like dead duplication, was load-bearing — then earned its
  collapse.** `runner_failures._FAILURE_CATEGORY_PATTERNS` looked like a
  removable duplicate of `failure_signatures.CATALOG`, but contract tests
  iterated it as the canonical set of classifier categories — removing it
  outright would have silently re-pointed a contract. The *correct* removal
  happened later, deliberately: the contract was re-pointed to
  `failure_signatures.CLASSIFIER_CATEGORIES` (derived from the catalog, one
  source of truth) and only then was the duplicate deleted. "Load-bearing"
  is a reason to re-point first, not a reason to keep forever.

The cheap, repeatable check: *can this protection actually fire, and does
changing it alter behavior a test or a real code path would notice?* Answer
that before classifying — for both keep and remove decisions.

The repo applies the same standard to its own enforcement: every lint rule
must demonstrate its fire path in a test (see
`tests/contract/test_lint_skills.py::test_lint_rule_fires_on_synthetic_input`
and `tests/scripts/test_lint_library_knowledge.py` — each rule is exercised
against a synthetic violation).

## The determinism boundary: judgment in the LLM, mechanism in verbs

An autonomous worker should perform only *genuine judgment* — the free-text
intent it relays (a campaign `goal`), long-tail classification a matcher can't
resolve, choosing among real candidate ambiguities. Every step whose outcome is
fixed by a rule belongs in a **composed verb**, not in skill prose the model
executes: authoring source or spec files, sequencing a deterministic verb chain,
resolving a field that has a known default, deriving a path. And every
agent-facing capability and contract must be reachable through a verb or a doc
the worker prompt points at — the worker must never read framework source (or
`inspect.getsource`) to learn a contract, nor hand-roll a capability the
framework already provides.

The enforcement is **removing the affordance**, not adding prose. Prose ("apply
a two-line edit", "do not invent a task_generator") is honor-system: the model
rationalizes around it under pressure. Observed failures that prose did not hold:
an `Edit`-tool decoration step that rewrote a scaffold's whole function body; a
fabricated `task_generator` justified by "autonomous mode applies safe_defaults";
a hand-sequenced classify pipeline mislabelled "in parallel" across a strict
producer→consumer dependency; a hand-rolled SLURM campaign controller and a
strategy contract reverse-engineered from site-packages source. Each is the same
root cause in a different face — **authoring / sequencing / discovery** — and
each fix takes the same shape: a bounded verb does the deterministic step, and
the tool or surface that allowed freelancing is removed (no `Edit` in onboarding
skills; the strategy is materialized by `scaffold-strategy`, not copied from
source; the preflight→classify chain is one `classify-axis-auto` call, not
hand-sequenced; the submit resolution applies safe-defaults via a deterministic
verb whose field partition refuses to fabricate a `task_generator`).

A guard the LLM itself satisfies is not a guard. A provenance marker claiming
"this task_generator was caller-supplied" was rejected for exactly this reason
(see "Verify a guard can actually fire") — the same model that fabricates the
value sets the marker. The lock is the missing affordance plus a deterministic
field partition (`ops/submit/field_partition.py`) whose `Ambiguity` refuses a
safe-default on a required-caller field — a guard that *can* fire.

### Enforcement map

Rows accrue per surface as the verbs land; the first two ship with the
`decorate-entry-point` surface.

| Rule | Enforced by | Fires when |
|---|---|---|
| Onboarding skills carry no `Edit` (decoration is a verb, not free-form source editing) | `tests/contracts/test_onboarding_skill_no_edit.py` | the `hpc-wrap-entry-point` skill's `allowed-tools` lists `Edit` |
| `decorate-entry-point` leaves the function body byte-identical | `tests/incorporation/test_decorate_entry_point.py::test_decorates_and_leaves_body_byte_identical` | the AST splice changes any line other than the inserted import + decorator |
| No raw `ssh`/`scp`/`rsync` affordance in agent-facing prose (remove the side channel that bypasses the connection-storm guards) — the affordance removed is the `inspect-deployment` companion: cluster reads go through a throttled verb, not raw ssh | `scripts/lint_no_raw_ssh.py` (CI + pre-commit), fire path pinned by `tests/scripts/test_lint_no_raw_ssh.py` | a bare `ssh`/`scp`/`rsync` invocation appears in a code span of a SKILL body or `worker_prompts/*.md` (a cited `ALLOWLIST` exempts a genuine human-debug doc) |

## Library knowledge in core: the four-question boundary test

hpc-agent's core is *experiment*-agnostic, not *software*-agnostic: it never
encodes what a user's parameters mean, but it legitimately knows scheduler
dialects, MPI launchers, pandas rolling idioms, and PETSc checkpoint hooks.
"It's already in core" is not the justification — passing this test is.
Knowledge of a specific third-party library may live in core only when ALL
four hold:

1. **Substrate, not semantics.** The knowledge is about how to run / persist /
   schedule / classify / verify computation — never about what an experiment's
   parameters or search space mean (those stay caller-owned: `tasks.py`,
   free-text `task_kind`, no typed search spaces).
2. **Core dispatches, never branches.** Library names appear in core only at
   *declared assembly points*. Everywhere else, core calls a library-agnostic
   contract (e.g. `checkpoint_formats.CheckpointFormat`, the axis-matcher
   dispatcher). Adding an assembly point is a reviewed edit to the lint's
   `KNOWLEDGE_PACKAGES` list, not an incidental import.
3. **Import-safe on every runtime surface it reaches.** There are three
   surfaces with different import budgets: the installed control plane
   (anything), the run's cluster env (installed package; stdlib-only modules
   preferred), and the standalone-shipped files (everything
   `transport._build_deploy_items` enumerates — they cannot import the
   package at all; duplication there is by design, see `_CHECKPOINT_RES`).
   Check the surface, not the repo.
4. **Core CI verifies it without the library installed.** Crafted fixtures
   (AST snippets, golden bytes like the PETSc Vec blocks) — if correctness is
   only testable with the real library, the knowledge belongs in a plugin
   whose CI carries the dependency, not in core.

When a knowledge family grows (a second solver adapter, a new matcher), the
rule is: collapse any inline library-name branching into the family's
registry/dispatcher, and add the new module behind it — do not add a second
inline branch.

### Enforcement map

| Rule | Enforced by | Fires when |
|---|---|---|
| Q2: declared assembly points only | `scripts/lint_library_knowledge.py` (CI + pre-commit) | any import binding a knowledge package — absolute, relative, lazy, or alias-form — outside the package or its declared list; also when a declared entry goes stale |
| Growth trigger: registry collapse at member #2 | same lint, "growth trigger" rule | a knowledge package reaches ≥ 2 member modules while a non-registry assembly point still binds a member module by name |
| Backend seam: orchestrator imports the interface, not a concrete backend (#337) | `scripts/lint_backend_boundary.py` (CI + pre-commit) | an orchestrator file (`ops`/`meta`/`recovery`/`incorporation`/`integration`) imports a concrete backend module (`infra.backends.{sge,slurm,sge_remote,slurm_remote,_engine,_remote_base,_scripts,query}`) — absolute, relative, lazy, or alias-form — instead of the seam re-exported from `infra.backends` (+ `remote_factory` / `profile`) |
| Q3: control-plane startup budget | `tests/contract/test_no_heavy_toplevel_imports.py` | a CLI-reachable module imports a heavy/solver library at module level |
| Q3: standalone files don't import the package | `tests/contracts/test_boundary_contract.py` (templates-don't-import-core) | a shipped template/standalone file references the core package. Adjacent but distinct: `scripts/lint_schema_versions.py` only syncs the cluster-side schema-version constants, and `_guard.py` is a runtime shadowed-import detector — neither statically enforces this row |
| Q4: core deps exclude the libraries themselves | `tests/contract/test_no_heavy_toplevel_imports.py::test_core_dependencies_exclude_heavy_libraries` | a banned library appears in `pyproject.toml` dependencies or any extra |
| Q1: substrate, not semantics | **judgment — review only** | a PR makes core interpret experiment parameters or search-space meaning; nothing mechanical catches this, which is why it leads the list |

### Drift log (why prose alone failed)

Recorded so the next "let's just document it" proposal has the base rate:
the `CLAUDE.md` predecessor of this page asserted three present-tense facts.
By 2026-06, `_FAILURE_CATEGORY_PATTERNS` no longer existed (collapsed into
`CLASSIFIER_CATEGORIES`; the prose still said "three tests iterate it") and
the deploy-ship list it cited omitted `executor_cli.py`. The lints and tests
from the same era all still held. Facts belong where they are checked; this
page cites sources of truth (`transport._build_deploy_items`, the lint's
`KNOWLEDGE_PACKAGES`) instead of restating their contents.

## Lifecycle verdicts and run identity: one definition, named tests

A run's terminal verdict (did it complete / fail / vanish?) and its dedup
identity (is this the same run; did its code drift?) are decisions that were
each historically re-derived at several call sites whose copies then disagreed —
the abandoned-vs-failed cluster (#351 #4: monitor, reconcile, and aggregate each
turned the reporter's counts into a verdict differently) and the executor-drift
replay that had to be fixed twice, once per dedup layer (#351 #5). The rule:
each such decision has exactly ONE definition that every call site routes
through, and the precedence it encodes is pinned by a property test, not a
comment.

Two corollaries the history earned:

- **The verdict is revisable; the evidence is durable.** Terminal states are NOT
  monotonic here — reconcile legitimately downgrades a premature `complete` to
  `failed` when new evidence arrives (#351 #4 *is* that correction). So do not
  add a "terminal is sticky" transition guard: it would re-break the bug it
  looks like it prevents. Record WHY each verdict was reached
  (`last_status.verdict_reason`, from `classify.settle`) so a wrong verdict is
  debuggable without re-running reconcile.
- **Centralize the decision, keep side-effects local.** `classify_polling` /
  `settle` decide; `_gather_failure_features` and `mark_run` stay at the call
  site. A pure decision over explicit evidence is testable without a cluster.

### Enforcement map

| Rule | Enforced by | Fires when |
|---|---|---|
| One count→verdict definition (poll + settle) | `tests/ops/monitor/test_classify.py` | a call site re-derives complete/failed/abandoned from raw counts instead of `classify_polling` / `settle` (`_is_terminal` is a thin adapter over the former) |
| Settle precedence: failure outranks absence; strict completion is never claimed while a failure is present (#351 #4) | `tests/ops/monitor/test_classify.py::test_settled_failure_outranks_absence`, `::test_settled_never_complete_while_failure_present` | a positive `failed` count reads as abandoned or complete |
| One executor/code-drift predicate for both dedup layers (#351 #5) | `tests/state/test_code_drift.py::test_layers_share_one_drift_predicate` | layer-1 (`runner._layer1_code_drift`) or layer-2 (`runs.find_run_by_cmd_sha`) re-inlines the drift comparison instead of routing through `state.code_drift.detect_code_drift` |
| Layer-1 dedup decision is pure + named (dedup/proceed/redo by status × drift × lever, #276 / #351 #5) | `tests/ops/submit/test_layer1_dedup.py` | the `submit_and_record` run_id-dedup tree changes behavior on any branch (terminal-failure proceeds, in_flight blocks, complete dedups / redoes-in-place / warns) without the unit test moving in lockstep |
| Verdict provenance is recorded | `tests/ops/monitor/test_classify.py::test_settle_carries_reason_and_evidence_for_each_arm` | `settle` stops carrying a reason + evidence snapshot for any arm |
| Polling-vs-settled completion divergence stays intentional | `tests/ops/monitor/test_classify.py::test_polling_and_settled_diverge_on_complete_with_stale_failure` | the lenient (mid-flight) and strict (settled) completion predicates are silently unified |
