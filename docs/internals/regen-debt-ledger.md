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

*(No outstanding debt — every prior row was verified paid on `main` and moved to
the "Checked" section below on 2026-07-15. The table header is kept so the gate
parses the empty-but-well-formed table; a new deferral adds a row in exactly
this format.)*

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
