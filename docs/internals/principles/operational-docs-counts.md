---
slug: operational-docs-counts
order: 10
title: "Operational docs: counts are verified live, never frozen"
scope: "A digit count of a counted set equals its live source of truth or sits in a cited allowlist; plus the regen-debt-ledger status pin."
---

# Operational docs: counts are verified live, never frozen

The operational-truth doc surfaces (`docs/internals` + `docs/workflows`)
narrate what the system IS now, not what it was — so a bare count of a
counted set (the primitive registry, the verb catalog, the shipped schemas,
the error-code enum, the regen-script set) is a fact that rots the instant
that set changes, and prose alone never notices. The rule: such a count
either equals the live number derived from its source of truth, or sits in a
cited allowlist for a deliberate historical reference. Design and plan docs
narrate history by design and are out of scope; fenced code blocks and
drift-log sections are masked (they legitimately carry the old numbers). The
count is line-based (a claim is a digit and its noun on one line) and
strict — no tolerance, because a two-off literal is exactly the drift that
slipped a prior ±2 pin.

## Enforcement map

| Rule | Enforced by | Fires when |
|---|---|---|
| A digit count of primitives, verbs, schemas, error codes, or regen scripts in `docs/internals` + `docs/workflows` equals the live count derived from its source of truth (registry for primitives/verbs, a recursive `schemas/**/*.json` glob, the `envelope.json` error-code enum, the `scripts/regen_all.py::REGEN_SCRIPTS` seam) or sits in `_COUNT_ALLOWLIST` with a cited reason; verbs means the registry count (the repo-prose convention), not the CLI-exposed subset | `tests/contracts/test_doc_frozen_counts.py::test_frozen_counts_track_live` (real-tree pin), `::test_frozen_count_check_fires_on_synthetic_violation` + `::test_frozen_count_check_passes_on_exact_and_masked` (fire/pass pair), `::test_count_allowlist_not_stale` + `::test_stale_allowlist_check_fires_on_synthetic_entry` (anti-stale), `::test_live_counts_are_sane` (vacuity floors); scope/masking seam `tests/contracts/_doc_scan.py` | an in-scope doc freezes a count the registry / schemas / error-code enum / regen set has since outgrown, or an allowlist entry's count catches back up to live (a dead exception) |
| A regen-debt-ledger row's status claim matches its live gate — the `## Outstanding regen debt` table in `docs/internals/regen-debt-ledger.md` parses under a strict 5-column header (format deviation = hard fail), every row names a runnable `test_*`/`tests/…​.py` gate (or the literal `no live gate`) that resolves under `tests/`, and a row marked `**RED**` whose named gate now PASSES hard-fails ("debt paid — remove the row"); a stale regen-debt note therefore cannot pass silently (devx A5) | `tests/contracts/test_regen_debt_ledger.py` (fires: a synthetic `**RED**`-claimed row whose gate passes → hard fail; a malformed header or prose-only gate cell → hard fail) | a ledger row's claimed gate state contradicts the live gate, a named gate stops resolving under `tests/`, or the table's 5-column format drifts |

## Drift log

`adding-a-primitive.md` opened with "the existing 167 primitives" while the
registry had already grown past it; the older ±2 primitives-only prose pin
sat at exactly its tolerance boundary on that line and let the two-off
literal pass. This section's strict, whole-family, line-based pin replaces
that tolerance for the operational surfaces; the ±2 pin was narrowed to the
out-of-scope `README.md` + `docs/reference/` surfaces it still uniquely
covers.
