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

## Reading this page

This page is the **index** to the engineering principles. The two judgment
rules that no lint enforces lead it in full — they are the constitution's
core and small enough to always read. Everything else is a **per-section**
file under [`principles/`](principles/): each is a self-contained unit —
prose + its own enforcement map (normative: the lint/test named in each row
is the source of truth) + any drift log kept together, so a row is never
severed from the rule it enforces.

**Navigate, don't read linearly.** Open the section you need from the
generated index below, or `grep` the `principles/` tree for a rule. The
section index is GENERATED from each file's frontmatter by
`scripts/build_principles_index.py` (a `regen_all` step) — do not hand-edit
the block between the markers; edit a section file's frontmatter and
regenerate with `python scripts/regen_all.py --write`.

**Read-what-you-touch.** Before you change an enforcement-mapped line, read
the section that owns it (not just this index); before classifying a guard
as intentional, removing an apparent duplication, or adding third-party
library knowledge to core, read the two judgment rules below.

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
`tests/contracts/test_lint_skills.py::test_lint_rule_fires_on_synthetic_input`
and `tests/scripts/test_lint_library_knowledge.py` — each rule is exercised
against a synthetic violation).

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

   The operational form of this rule, earned by four features that each faced
   the same cut (opaque evidence scopes, declared-assertion anchors, the
   dossier manifest, spec registration — see
   `docs/design/rigor-primitives.md`): **core's agnostic surface is IDENTITY,
   ORDERING, COMPARISON, and COUNTING over opaque caller content.** Anything
   that decomposes into those four operations (hash it, gate on record order,
   compare numbers under a caller tolerance, count records per opaque tag) can
   live in core; the moment a design needs core to *name* what the content
   means — a field called "holdout", "placebo", "units", a default tag, a
   recognized metric — it belongs in a domain pack or the caller's repo. The
   crossing point is vocabulary, not mechanism.
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
| Plugin→core API surface: the CI-gated notebook-render example plugin imports only the declared allowlist (W3/P5b) — the pinned surface is `docs/reference/plugin-api-contract.md` (contract v1) | `scripts/lint_plugin_api_surface.py` (CI `lint` job; `--fire-path` in the `plugins` job), fire paths pinned by `tests/scripts/test_lint_plugin_api_surface.py` | the plugin imports an `hpc_agent.*` module/symbol not in `ALLOWED_PLUGIN_IMPORTS` (stay-inside — the scan `ast.walk`s the whole tree, so top-level, `TYPE_CHECKING`, and function-local imports all count), OR an allowlisted module/symbol no longer resolves in core (anti-drift — a reorg moved/renamed/dropped it), OR an allowlist entry is unused by the plugin (a dead row) |
| W2: a leading-underscore symbol stays package-private (no cross-package private API) | `scripts/lint_private_cross_package_imports.py` (CI + pre-commit), pinned by `tests/scripts/test_lint_private_cross_package_imports.py` | a file under `src/hpc_agent` does `from hpc_agent.X import _name` — absolute or relative, lazy or top-level — where `_name` is a single-underscore private symbol (not a dunder, not a private submodule on disk) whose owning package is not the importer's, and the `importer :: module :: _name` triple is not in the shrink-only ledger `scripts/private_cross_import_allowlist.txt`; ALSO when a ledger entry goes stale (its import no longer exists — the ledger only shrinks) |
| Q3: control-plane startup budget | `tests/contracts/test_no_heavy_toplevel_imports.py` | a CLI-reachable module imports a heavy/solver library at module level |
| Q3: standalone files don't import the package | `tests/contracts/test_boundary_contract.py` (templates-don't-import-core) | a shipped template/standalone file references the core package. Adjacent but distinct: `scripts/lint_schema_versions.py` only syncs the cluster-side schema-version constants, and `_guard.py` is a runtime shadowed-import detector — neither statically enforces this row |
| Q4: core deps exclude the libraries themselves | `tests/contracts/test_no_heavy_toplevel_imports.py::test_core_dependencies_exclude_heavy_libraries` | a banned library appears in `pyproject.toml` dependencies or any extra |
| Q1: substrate, not semantics | **judgment — review only** | a PR makes core interpret experiment parameters or search-space meaning; nothing mechanical catches this, which is why it leads the list |

### Drift log (why prose alone failed)

Moved to the companion history page:
[`engineering-principles-history.md` § Drift log](engineering-principles-history.md#drift-log-why-prose-alone-failed).
In short: the `CLAUDE.md` predecessor of this page asserted three present-tense
facts and two had silently rotted (`_FAILURE_CATEGORY_PATTERNS` collapsed into
`CLASSIFIER_CATEGORIES`; the deploy-ship list omitted `executor_cli.py`) while
every mechanized check stayed true — which is why this page cites sources of
truth (`transport._build_deploy_items`, the lint's `KNOWLEDGE_PACKAGES`) instead
of restating their contents.

## Sections

Each row links a self-contained section file. Sizes are approximate.

<!-- BEGIN GENERATED SECTION INDEX -->
| Section | Scope | Size |
|---|---|---|
| [The determinism boundary: judgment in the LLM, mechanism in verbs](principles/determinism-boundary.md) | Judgment stays in the LLM; every rule-fixed step is a composed verb, enforced by removing the affordance. | ~1.8k tokens |
| [Lifecycle verdicts and run identity: one definition, named tests](principles/lifecycle-verdicts.md) | One definition per terminal-verdict / run-identity / run-execution decision, every call site routing through it. | ~27.1k tokens |
| [The registration kernel: the deployment-boundary attestation is mechanism-only](principles/registration-kernel.md) | The deploy-boundary promotion is one more attestation over the sealed dossier — mechanism-only, agnostic by five mechanisms. | ~1.4k tokens |
| [The determinism fingerprint: measure, don't ask](principles/determinism-fingerprint.md) | A measured, confidence-labeled run-to-run spread — core measures and compares, never names a metric or invents a tolerance. | ~1.5k tokens |
| [Domain packs: bind-as-data, trust content-addressed](principles/domain-packs.md) | Bind pack files by sha, gate on pack receipts, never run or interpret pack logic; plus the evidence-memory and challenge boundaries. | ~4.0k tokens |
| [Live conformance: the chart judges, the operator adjusts](principles/live-conformance.md) | SPC on the attestation substrate — observe, judge, route; every actuation stays the operator's, outside this system. | ~1.9k tokens |
| [Multi-human: attributed, never verified](principles/multi-human.md) | An opaque harness-asserted actor core compares but never verifies — byte-identical under zero/one declared actor. | ~2.1k tokens |
| [Repo mechanics: the generated-artifact merge driver keeps ours, never silently drops theirs](principles/repo-mechanics-merge-driver.md) | A keep-ours merge driver for 100%-generated files only; a partially generated file must never carry it. | ~0.6k tokens |
| [The MCP in-process runner: never touch the real transport streams](principles/mcp-in-process-runner.md) | In-process dispatch swaps out all three real stdio streams; session stream reconfig happens once, before the reader thread exists. | ~0.6k tokens |
| [Operational docs: counts are verified live, never frozen](principles/operational-docs-counts.md) | A digit count of a counted set equals its live source of truth or sits in a cited allowlist; plus the regen-debt-ledger status pin. | ~0.9k tokens |
| [The CLI single-verb fast path: byte-identical to the full walk, or it must not run](principles/cli-fast-path.md) | Fast-path opt-in is an enumerated set; discovery verbs answer off the content-keyed bake or take the full walk — never a partial registry. | ~1.1k tokens |
<!-- END GENERATED SECTION INDEX -->

## Drift log

**2026-07-16 — per-section split (devx B2).** This page was a single ~31k-token document that exceeded the single-read cap of common agent tooling. It was split along its self-contained `##` sections into `principles/<slug>.md` (one file per section: prose + enforcement map + drift log), leaving this page as the GENERATED index plus the two unsplittable judgment rules in full. The deferred P6.4 row-level split — the earlier judgment that relocating enforcement rows away from their prose was too risky for one pass — is now moot: every enforcement row moved WITH its section, and the orphaned run-13 fix-swarm rows that had accreted after the last drift log were re-homed into their owning sections. Section history predates the split in THIS file: run `git log --follow -- docs/internals/engineering-principles.md` for the pre-split blame, and the narrative/drift-log HISTORY still lives in [`engineering-principles-history.md`](engineering-principles-history.md).
