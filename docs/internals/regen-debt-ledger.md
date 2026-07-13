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
the intended end state is a **strict-xfail punch-list** — a CI ledger test
(precedent: `tests/ops/recover/test_recovery_registry.py`) that fails loudly
once a listed rebake is overdue, so an out-of-date regen note cannot pass
silently. That test does **not** exist yet; until it does, this doc is the
manual record and each item's own contract/roundtrip test is the live gate.

## Outstanding regen debt

| Item | Source drift log | What is owed | Live gate today | Owner / wave |
|---|---|---|---|---|
| `ScopeKind` literal + `verify-registration` verb | `design/registration-kernel.md` (T5/T6 seam) | The six regen scripts (`operations.json` registry count, indices, frontmatter) — explicitly **NOT run** ("deferred per the Wave-C dispatch"). | `tests/_wire/test_schema_models_roundtrip.py` GREEN as landed. | registration / Wave-C |
| `observables` field on the `interview` `_AuditedSource` + `NotebookRecordConfigSpec`/`Result` schemas | `design/data-trace.md` (Amendment 14, B-series) | Additive schema field; **wire/regen deferred to the serial rebake**. No `TRACE_SCHEMA_VERSION` bump (readers tolerate the new key). | Present-only key; readers tolerant. | data-trace |
| `ReproductionReceipt.stage_interlock` + `.diverged_stage`; `VerifyReproductionResult.diverged_stage` | `design/data-trace.md` (Amendment 15, fingerprint interlock) | Schema regen **NOT run** ("serial-regen discipline — rebake at merge"). Optional/default-absent so pre-interlock lines parse unchanged. | Byte-identical for untraced pairs (pinned by test). | data-trace |
| `NotebookSectionView.trace_summary` wire mirror | `design/data-trace.md` (Amendment 16, `trace_summary`) | The structured wire result carries no `trace_summary` mirror; the field is **deferred wire debt for the serial rebake**. | Regen `--check` GREEN (registry unchanged at 164; nothing wire-facing moved). | data-trace |
| `trace-render` (T5) registry entry | `design/data-trace.md` (2026-07-08 drift line) | "registry +1; regen deferred to a serial rebake." | Registry arithmetic relative; regen deferred. | data-trace |
| Inherited evidence-memory / pack schema drift (`evidence_brief`, `evidence_period`, `pack_*`, `resolve_submit_inputs.output.json`) | `design/challenge-attestation.md` (T8, "Schema regen debt (inherited)") | Missing `_CROSS_FIELD_OVERRIDES` entry the evidence author owns; **left for the evidence-memory/pack merges' regen** — NOT challenge scope. | `test_schema_models_roundtrip[evidence_brief.input.json]` **RED** on the branch. | evidence-memory / pack merges |
| `notebook-draft` verb + `conformance-record` template | `design/multi-human.md` (MT-series, "Regen debt at landing") | Two inherited contract failures: `test_spec_verb_inventory_matches_cli` (`notebook-draft` absent from `_SPEC_VERBS` — MT5 new-verb regen) and `test_lint_primitive_doc_templates` (`conformance-record` template mismatch — Phase-8 conformance wave). | Both tests **RED** on the branch; noted so the next regen pass clears them. | MT5 / Phase-8 conformance |

## Checked — no outstanding debt (recorded so nobody re-opens them)

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
   `python scripts/build_operations_index.py` (and the sibling `build_*` scripts)
   without `--check`, committing the regenerated files.
2. Re-run the item's live gate (the roundtrip / contract test named above) and
   confirm it is GREEN.
3. Remove the row here **and** collapse the originating drift-log note to a
   one-line "paid — see the ledger" pointer.
