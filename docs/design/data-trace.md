# The data trace — stage receipts for the pipeline (the audit's runtime twin)

**Status: PLANNED, USER-RULED (2026-07-08, all five load-bearing questions
resolved).** The product one-liner applied WITHIN a run: "what changed
between stage N and N+1, answered mechanically instead of by archaeology."
Motivating incidents are run #10's, same night: the 0.13242-vs-0.120 window
question answered by agent narration; 246,059→218,905 row accounting
reconstructed from memory; the arm-alignment inner-join eyeballed; the
canary-exclusion count a standing manual watch-item across three runs.

## Design center

The audit template's sections answer "what does the code SAY it does"
(static, signed). The trace answers "what did the data actually DO"
(dynamic, per-run) — stage receipts emitted INLINE at stage exit, never
reconstructed post-hoc (a reconstructed trace is a story again; an inline
trace is evidence: transient intermediates, identity binding to the run
that produced the number, cheap-at-the-moment counts).

## The record (one JSONL line per stage exit)

```
{stage, section?, seq, rows_in, rows_out, rows_dropped, drop_reason?,
 cols_added, cols_dropped, units_space?, nan_by_col?, avail_windows?,
 time_integrity {monotonic, dups, gaps}, sketch {col: {mean,std,q05,q95,
 min,max}}, wall_ms, peak_mb?, digest?, created_at}
```

- **`stage` vs `section` (ruling Q3):** `stage` is the fine-grained emit —
  ONE code-level transformation whose effect you'd want isolated in a diff
  (diurnal-adjust / sqrt / winsorize are THREE records). `section` is the
  OPTIONAL audit-template slug housing them (one section : many stages).
  Both opaque to core. "Atomic" is deliberately a pack GUIDANCE with a
  stated intent, never an enforced rule (it is not well-defined).
- **Units ledger:** `units_space` stamps the target's space per stage
  (pack vocabulary: raw-var / diurnal-div / sqrt / winsorized / smeared-raw)
  — the #1 historical mirage class (units/target bugs) becomes a visible
  chain; an inversion reads as a broken link in the render.
- **Row conservation:** the render enforces
  `rows_out == rows_in - rows_dropped` and flags violations loudly —
  silent row loss becomes impossible to miss. Every drop carries a reason.
- **Column lineage:** cols_added per stage → the render's
  feature→birth-stage table.
- **Missingness structure:** per-column NaN counts + per-family
  availability windows (first/last valid date — the silent sample
  constraint), fill counts at fill stages (the overnight-fill artifact,
  permanently visible).
- **Excluded by design:** data VALUES (timestamps at head/tail only —
  a trace is shareable evidence, never a data leak) and judgment fields
  (no "looks wrong": the trace shows, the scientist concludes — the
  pointing doctrine applied to data).

**Stage zero = input identity:** the data-manifest shas (rung 0), closing
the chain end-to-end: which bytes in, what happened at every step, what
number out.

## When it is captured (all execution contexts)

At STAGE EXIT, inline, in: the local gauntlet run ("did my cheap-kill see
what I think?"), the canary (trace-diff canary-vs-local catches
deploy/data divergence in one glance), every array task (arm-keyed —
"did both arms see identical rows?"), and **the reduce/aggregate step** —
pooling and canary-exclusion are data transformations too; "exactly N rows
canary-excluded" becomes a trace record instead of a per-run eyeball item.

## Digest policy (ruling Q2 — NO USER KNOB, context decides)

A per-stage/per-run digest flag is an attention tax on a sparse resource.
Counts/sketches are always on (~free next to a walk-forward). Content
digests of intermediates are enabled AUTOMATICALLY by context: ON where
identity is what the run is for — the canary, `reproduce-run` derived
runs, the local gauntlet (all 1-to-few tasks); OFF in wide arrays (unless
the run IS a reproduction). The machinery classifies; the human never
configures. (Mechanize the simple mass — the auto-mode pattern.)

## The fingerprint interlock (ruling Q1 — yes; lands with Phase 3)

Stage digests are fingerprint-admissible evidence from day one: the
envelope accrues per-stage, and a reproduction mismatch localizes to a
NAMED STAGE ("diverges at scaling") instead of "the runs differ". This is
a Phase-3 amendment (the sample-admission model gains per-stage keys);
the projections below are freestanding and do not wait for it.

## Projections (four views over one stream + one diff)

1. **Row waterfall** — stages × counts, conservation-checked.
2. **Units ledger line** — the space chain, round-trip visible.
3. **Feature lineage** — column → birth stage.
4. **Target sketch table** — per-stage distribution of declared columns
   (how you SEE winsorization bite / a scale guard fail to fire).
5. **Trace diff** — two runs overlaid; the FIRST stage where any view
   diverges is highlighted. Canary-vs-local, arm-vs-arm, today-vs-last-
   known-good.

All code-rendered, deterministic, trusted-display class (the LLM points).
Pull-only; NO alarms — the trace feeds briefs/verdicts only through the
existing surfaces (D8: route only what blocks).

## Storage (ruling Q5 — one rule, unified)

**The trace lives WITH the output it explains**: it rides the
`$HPC_RESULT_DIR` contract wherever that contract exists (after run-#10's
F-C fix, that is THE output rule of the system) — the emitter writes
`<result>/_trace.jsonl`, the existing harvest brings it home, projections
read harvested traces. `.hpc/traces/<id>/` is the FALLBACK for output-less
contexts only (e.g. an audit-prelude execution). No third location, no new
transport, no SSH.

## Outsourcing due-diligence (ruling Q4 — adopt-if-better, FIRST TASK)

Others likely do parts of this better; the plan's first task is a gate,
not a formality (the filelock/psutil precedent): evaluate **OpenLineage**
(record/facet shapes; column-level lineage), and the adjacent field
(Great Expectations — the validation half; DVC / Hamilton / dagster —
asset lineage) against the HARD constraints: append-only JSONL, no
daemon/server, stdlib-only core (pandas awareness lives pack-side),
sha-bindable records, journal-native. IF a standard's record shape fits,
ADOPT THE SHAPE ITSELF (vocabulary and facets included), not merely an
export adapter; if not, minimal-ours + an export adapter in the
conformance-kit lane, with the refusal reasons recorded here. The
evaluation's verdict amends this doc before implementation starts.

## Layer split

| Layer | Owns |
|---|---|
| Core | record format (opaque slugs, counts, digests), harvest transport (none new — rides results), the render + diff projections. Stdlib-only. |
| Quant pack | the pandas-aware EMITTER library, the stage vocabulary + section mapping convention, per-stage invariant conventions (the D-V family at runtime: "shift changes rows by exactly h", "burn-in = max_lag"), units-space vocabulary |
| Program | `trace.emit(...)` calls at stage boundaries (executor/models — a one-time instrumentation pass) |

## Sequencing (the cheapest adoption curve)

1. **Outsourcing gate** (above) — amends this doc.
2. **Instrument harxhar with a pack-side emitter draft** — caller-side
   JSONL needs ZERO core code; the files are immediately readable with
   pandas. The record shape gets proven against reality before core
   freezes it.
3. **Core projections** (render + diff verbs) land on already-flowing
   data; registry +1 or +2.
4. **Fingerprint interlock** as a Phase-3 amendment.

Enforcement: toy fixtures only in core tests; the never-judgment pin (the
render contains no verdict vocabulary — grep-testable); the pointing
doctrine (renders relayed verbatim).

## Drift log

- 2026-07-08: written (Fable); user rulings Q1 yes / Q2 no-knob
  context-automatic / Q3 stage-finer-than-section with optional mapping /
  Q4 adopt-if-better with a due-diligence gate / Q5 trace-rides-the-
  output-contract folded.
