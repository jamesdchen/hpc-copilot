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
| `docs/design/notebook-audit.md` (Drift log, 2026-07-30 — the sequencing reversal) — the `audit` block chain gave five verbs a `stage_reached` / `needs_decision` / `next_block` triple (plus `notebook-status` a sign-off `brief` + chained `review` pointers), `audit-preflight` a `source` input, `notebook-lint` an `audit_id` seat, and `block-drive` an `audit` seat sub-model; the same wave added an enforcement row to `docs/internals/principles/determinism-boundary.md` | `docs/design/notebook-audit.md` | ONE `python scripts/regen_all.py --write` pays BOTH halves. (a) `build_schemas` — 9 files: `audit_preflight.input.json`, `audit_preflight.output.json`, `block_drive.input.json`, `notebook_lint.input.json`, `notebook_lint.output.json`, `notebook_auto_clear.output.json`, `notebook_audit_view.output.json`, `notebook_status.input.json`, `notebook_status.output.json`. (b) `build_principles_index` — the new determinism-boundary row moved that section's generated size cell in `docs/internals/engineering-principles.md`. The remaining 7 regen steps (operations.json, frontmatter, both primitive/operations indices, verb-module map, harness runbook, pending-docs check) are GREEN on this branch and are NOT owed | **RED** — `tests/_wire/test_schema_models_roundtrip.py::test_emitted_schema_matches_checked_in`, `::test_minimal_instance_validates_against_emitted_schema`; `tests/scripts/test_build_principles_index.py::test_real_tree_index_is_up_to_date`, `::test_check_fires_red_on_header_change_then_write_heals` | prelude Wave P2.b (`docs/plans/prelude-chain-2026-07-30.md`) — pay at the wave's ONE serial rebake |
| `cluster-readiness` verb (new `query` primitive + its two wire models) | `docs/design/s2-readiness.md` (Drift log, 2026-07-30 — pillar 1) | One serial `regen_all --write`. **6 of the 9 steps are stale**: `build_schemas`, `bake_operations_json`, `build_primitive_frontmatter`, `build_primitive_index`, `build_operations_index`, `build_verb_module_map` (`build_principles_index`, `build_harness_runbook` and `check_no_pending_primitive_docs` PASS). **8 files land**: `src/hpc_agent/schemas/cluster_readiness.input.json` + `.output.json` (new), `src/hpc_agent/operations.json`, `docs/primitives/cluster-readiness.md` (frontmatter block only — the body is hand-written and complete), `docs/primitives/README.md`, `docs/generated/operations.md`, `src/hpc_agent/cli/_verb_module_map.py`. Deferred by construction: the wave lands the code and defers the bake so concurrent S2 waves don't race the same generated artifacts. `scripts/lint_wire_suffix.py` is red for this one reason (both models resolve to a schema file not yet emitted) and goes green with the bake; all 25 other gauntlet lints PASS. | **RED** `tests/_wire/test_schema_models_roundtrip.py` — exactly 4 params fail, two each of `test_emitted_schema_matches_checked_in` and `test_minimal_instance_validates_against_emitted_schema`, parametrized on cluster_readiness.input.json and cluster_readiness.output.json (the other 500 params in that file pass). Already GREEN and pinning the rest of the surface: `test_spec_verb_inventory_matches_cli`, `tests/cli/test_cli_completeness.py`, `tests/contracts/test_doc_frozen_counts.py`. **NOT covered by this row:** the 2026-07-30 rsync coupling (B1) is a code/test fix on this branch, not regen debt — `tests/infra/test_remote_rsync_fallback.py::test_large_delta_push_folds_every_checkpoint_and_the_final_seal` is GREEN here (4 legs) and its 6-vs-4 failure is fixed, not deferred. Caveat on the negative controls: 3 of this wave's assertions are skips or environment-gated rather than live pins — the two `readiness_sensors` lockstep cases `importorskip` until the S2 branch merges, and `test_the_default_journal_home_is_not_inside_a_repo_tree` asserts a path shape rather than exercising a push; the slow-tier full-suite run is where a real cross-substrate regression would surface. | s2-readiness pillars 1+6 |
| `wrap-entry-point-auto` input grew `data_axis_hint` + `solver` (the two wrapper-only interview fields, #260/G2+G5) | `docs/plans/prelude-chain-2026-07-30.md` (Wave P1, unit P1.a — skill-rewiring seam pass) | `scripts/build_schemas.py --write` re-emits `schemas/wrap_entry_point_auto.input.json` (two new optional fields + the `_DataAxisHint` / `_HaloHint` / `_PetscSolverHint` `$defs` reused verbatim from `interview.input.json`) | **RED** `tests/_wire/test_schema_models_roundtrip.py::test_emitted_schema_matches_checked_in[wrap_entry_point_auto.input.json]`; also red until the rebake: `test_minimal_instance_validates_against_emitted_schema` (the stale `additionalProperties: false` schema rejects the model's own minimal dump) | P1.a seam-gap pass / Wave P1 |
| `wrap-entry-point-auto` `needs_wrapper_argv` grew `argv_extraction` + `argv_params` (the carried extraction, G1) | `docs/plans/prelude-chain-2026-07-30.md` (Wave P1, unit P1.a — skill-rewiring seam pass) | `scripts/build_schemas.py --write` re-emits `schemas/wrap_entry_point_auto.output.json` (two new fields on the `_NeedsWrapperArgvResult` branch of the four-shape `anyOf`) | **RED** `tests/_wire/test_schema_models_roundtrip.py::test_emitted_schema_matches_checked_in[wrap_entry_point_auto.output.json]` | P1.a seam-gap pass / Wave P1 |

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
