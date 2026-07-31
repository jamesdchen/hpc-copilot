# Regen-debt ledger

One place to see every **outstanding "rebake at merge" / regen-debt** item that
individual design drift logs deferred. Regen (the six `scripts/build_*.py`
generators — `operations.json` registry, the indices, the frontmatter, and the
wire-schema roundtrip fixtures) is run **serially** across concurrent design
waves: a wave that changes a wire shape or the verb registry often lands its
code with the actual `--check` regen **deferred to a later serial rebake**, so
two waves don't race the same generated artifacts. That deferral is *regen
debt*. Untracked, it rots: a stale generated file ships, or a merge silently
clobbers one wave's regen with another's.

This ledger consolidates those deferrals so an unpaid rebake is visible in one
place instead of buried across six drift logs. Each design drift log remains the
**authoritative narrative** for its item; this table is the index.

Per the architecture review (P6.8, paired with N6's deprecation-expiry idiom),
this ledger is a **strict-xfail punch-list** held by a CI test
(`tests/contracts/test_regen_debt_ledger.py`; precedent:
`tests/contracts/test_recovery_registry.py`). The test parses the table below
(strict 5-column header — any format deviation is a hard failure, so the format
can't silently break the parser), verifies every row's named live gate exists
under `tests/`, and executes each `**RED**` row's gate: a still-failing gate
`xfail`s (debt outstanding, suite stays green) while a now-passing one HARD
FAILS ("debt paid — remove the row"). An out-of-date regen note therefore
cannot pass silently.

## Outstanding regen debt

| Item | Source drift log | What is owed | Live gate today | Owner / wave |
|---|---|---|---|---|

Row format (binds every future row):

- **Live gate today** must carry at least one backticked pytest reference
  (a `test_*` function name or a `tests/…​.py` path) OR the literal
  `no live gate`. A named `test_*` must resolve under `tests/` (function
  definition or file stem).
- Mark a row `**RED**` in the **Live gate today** cell only when its named
  gate is a runnable target that is *currently failing* on the branch — that
  is the strict-xfail punch-list state. A `no live gate` row may NOT be marked
  `**RED**` (there is nothing to xfail — it is a hard format error).

## Checked — no outstanding debt (recorded so nobody re-opens them)

Paid down 2026-07-30 (U5, the missing-combiner unit — `regen_all.py --write`
run in-branch, 9/9 PASS). U5 adds the `redeploy-runtime` primitive (a `mutate`
verb with a `CliArg` shape and no wire spec model), so the regen touched
`operations.json`, `docs/primitives/{redeploy-runtime.md,README.md}`,
`docs/generated/operations.md`, and `cli/_verb_module_map.py`; **no JSON schema
was added or changed** (the verb takes flags, not a `--spec` payload, so
`build_schemas` reported "up to date, 253 models"). The `combiner_missing`
recovery kind and `errors.CombinerMissing` add NO wire surface: the kind is a
prose-only registry entry, and the error subclasses `CombinerFailed` so the
envelope `error_code` enum is untouched.

- U5 (`execution/mapreduce/deployed_artifact.py`, this ledger's own wave) —
  `redeploy-runtime` (a repair verb re-shipping the framework runtime to a
  run's base `remote_path` and, when §10.S4 pinned one, its code tree — the
  two roots serve the control plane and the job respectively) registry row +
  frontmatter + indices + verb map. Gates
  `tests/contracts/test_recovery_registry.py`,
  `tests/contracts/test_lint_primitive_doc_templates.py` and
  `tests/test_errors.py` GREEN; `regen_all.py --check` GREEN in-branch. **A
  concurrent wave that also rebakes must re-run the serial regen at
  integration** — this row records that it was already run once here, not that
  the artifacts can't be clobbered by a later merge.

Paid down 2026-07-30 (Wave P2 integration's one serial rebake —
`regen_all.py --write`, all 9 steps PASS; strict-xfail tripwire in
`tests/ops/test_audit_chain.py` deleted in the same commit):

- `docs/design/notebook-audit.md` (sequencing reversal, Wave P2.b) — 9 audit
  chain schemas + the determinism-boundary principles index resize. Gates
  `test_schema_models_roundtrip` + `tests/scripts/test_build_principles_index.py` GREEN.
- `docs/design/s2-readiness.md` (pillar 1) — `cluster-readiness` verb schemas,
  operations.json, frontmatter, indices, verb map. Gate
  `test_schema_models_roundtrip[cluster_readiness.*]` GREEN.
- `docs/plans/prelude-chain-2026-07-30.md` (P1.a seam-closure G1/G2/G5) —
  `wrap_entry_point_auto.{input,output}.json` re-emit. Gates GREEN.

Paid down 2026-07-30 (Wave P1 integration's one serial rebake —
`regen_all.py --write`, all 9 steps PASS):

- `docs/plans/prelude-chain-2026-07-30.md` (Wave P1, unit P1.c) — `interview`
  `_ShellCommandEntry.run_name` optional → `schemas/interview.input.json`
  re-emitted. Gate `test_schema_models_roundtrip[interview.input.json]` GREEN.
- `docs/internals/principles/multi-human.md` (Drift log, 2026-07-30) — section
  grew the P1.c actor-module extension; principles section index re-emitted.
  Gate `tests/scripts/test_build_principles_index.py` GREEN.

Paid down 2026-07-15 (verified on `main`: all six regen `--check` gates GREEN,
`build_verb_module_map --check` GREEN, and each item's named live gate GREEN —
see the per-item pointers). The originating drift-log notes were collapsed to a
one-line "paid — see the ledger" pointer per the pay-down procedure below.

- `design/registration-kernel.md` (T5/T6 seam) — `ScopeKind` literal +
  `verify-registration` verb regen. Gate: `scripts/*.py --check` +
  `tests/_wire/test_schema_models_roundtrip.py` GREEN.
- `design/data-trace.md` (Amendment 14, B-series) — `observables` field on
  `interview` `_AuditedSource` + `NotebookRecordConfigSpec`/`Result`. Gate:
  `test_schema_models_roundtrip` GREEN (additive key; readers tolerant).
- `design/data-trace.md` (Amendment 15) — `ReproductionReceipt.stage_interlock`
  / `.diverged_stage` + `VerifyReproductionResult.diverged_stage`. Gate:
  `test_schema_models_roundtrip` GREEN (optional/default-absent; untraced pairs
  byte-identical).
- `design/data-trace.md` (Amendment 16) — `NotebookSectionView.trace_summary`
  wire mirror. Gate: regen `--check` GREEN (registry unchanged).
- `design/data-trace.md` (2026-07-08 drift line) — `trace-render` (T5) registry
  entry. Gate: `bake_operations_json --check` + `build_operations_index --check`
  GREEN (registry at 169).
- `design/challenge-attestation.md` (T8, inherited evidence-memory / pack
  schema drift: `evidence_brief`, `evidence_period`, `pack_*`,
  `resolve_submit_inputs.output.json`). Gate:
  `test_schema_models_roundtrip[evidence_brief.input.json]` GREEN.
- `design/multi-human.md` (MT-series) — `notebook-draft` verb +
  `conformance-record` template. Gate:
  `tests/contracts/test_primitive_remediation.py::test_spec_verb_inventory_matches_cli`
  (`notebook-draft` now in `_SPEC_VERBS`) +
  `tests/contracts/test_lint_primitive_doc_templates.py` GREEN.

Recorded earlier (still checked, nobody re-opens):

- `design/mcp-elicitation.md` (E-render + same-day amendment): "regen debt: none
  — same class as E6; the orchestrator's central regen run confirms byte
  stability." No new primitive, no wire-model change.
- `design/notebook-audit.md` (plan-throughput concurrency modes): none for
  `operations.json` (no `@primitive` signature changed) and no JSON schema exists
  for `plan-throughput` output, so the three new envelope keys add no schema
  regen. Re-run the standard regen + full suite to confirm on the next pass.

## Paying down an item

1. Land the concerned wave's code, then run the full regen serially **after** any
   concurrent wave that also touches generated artifacts:
   `python scripts/regen_all.py --write` (the single entry point that runs all
   six generators in dependency order plus the pending-docs check), committing
   the regenerated files.
2. Re-run the item's live gate (the roundtrip / contract test named above) and
   confirm it is GREEN.
3. Remove the row here **and** collapse the originating drift-log note to a
   one-line "paid — see the ledger" pointer.
