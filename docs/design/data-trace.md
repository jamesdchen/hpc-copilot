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

## Amendment 1 (2026-07-08, user-ruled): the ATOM CATALOG restructure

The original record/layer sections above leaked program vocabulary into the
pack tier (a units ledger of raw-var/sqrt is RV's chain; "shift drops
exactly h" is a program invariant). Ruled restructure — the D-V1
three-level vocabulary applied to measurement itself:

**Core owns ATOMS**: named, typed, meaning-free measurements, EACH WITH ITS
COMPARISON SEMANTICS (what makes the diff engine discipline-generic):
`row_count` (exact + the generic conservation invariant), `col_set`
(set-delta), `null_count[col]` (exact per key), `value_sketch[col]`
(tolerance), `span[col]` (endpoints), `order_integrity[col]`
(monotonic/dups/gaps-vs-declared-grid), `label_chain` (equality along the
chain — the units ledger GENERALIZED: core knows "a tracked label", never
"units"), `digest` (exact), `duration_ms`/`peak_mb` (tolerance). Record
container: {stage, section?, seq, atoms{}, flags[]}. Core invariants are
GENERIC ONLY (row conservation, chain continuity, seq monotonicity) — NO
invariant DSL: core never evaluates pack-authored expressions; pack/program
invariants are checked IN THE EMITTER and recorded as opaque `flags` core
renders but never interprets. Render/diff operate on atom kinds, so any
discipline's composition renders/diffs unmodified.

**The measurement protocol**: core defines each atom's input contract
(shape-validated floats/ints/sets); the pack's pandas-aware emitter is the
implementation that measures frames and emits — core validates shapes,
never touches frames (the receipts seam: caller executes, core binds).

**Quant pack composes**: stage-CLASS vocabulary (load/transform/feature/
split/fit/score — quant-general, never RV's stage names), which atoms per
class, the `units_space` label_chain instance + claimed-units doctrine,
class-altitude invariants parameterized (never h=1).

**Program binds**: concrete stages to classes, actual labels, parameters,
program invariants as emitter checks → flags.

Altitude test both ways applies to every future atom/composition: "would a
second program adopt it unedited?" and "would a second DISCIPLINE adopt it
unedited?" — an atom failing the second test belongs in a pack, not core.

## Amendment 2 (2026-07-08): the digest classifier, mechanically

Q2 expanded. Digests have exactly ONE consumer — identity questions
(reproduction verification, canary-vs-local, fingerprint admission) — and
whether a run IS one is already recorded before it starts. The classifier
is a pure function of the run's own sidecar: canary flag / `reproduces`
field / local-gauntlet context / task_count. Implementation: the DISPATCHER
reads the sidecar and exports HPC_TRACE_DIGESTS into the task env — code
sets the flag, the human never sees a decision point. FAILURE POSTURE
(what makes knob-removal safe, not just convenient): on-when-unneeded =
bounded seconds wasted; off-when-needed = verification DEGRADES to
whole-run comparison and DISCLOSES "stage digests unrecorded" — the status
quo plus honesty, never a block, never a fabricated match. A spec-level
override exists (force_on/force_off) but is an OVERRIDE, never a prompt,
and its exercise is disclosed (the caller-tolerance posture reused). The
classifier's mapping is human-owned frozen code — changing the CLASS is a
reviewed edit; instances never ask; nothing adapts. (Third instance of the
pattern tonight: auto-clear tiers, tiered verdicts, digest policy — the
run's recorded identity determines its observation level.)

## Amendment 3 (2026-07-08, user-directed): atomicity DEFINED, not conventioned

Q3's "atomic is a guidance" is superseded — atomicity is definable and
TESTABLE, as a two-argument property: relative to (a) the atom catalog and
(b) a DECLARED DEFECT SET (the bug classes the trace must localize):

> A partition is LOCALIZATION-COMPLETE when every declared defect, if
> present, first alters the atoms of exactly one stage's record; MINIMAL
> when no boundary can be removed without merging two defects into one
> stage. An ATOMIC STAGE is an element of a minimal localization-complete
> partition.

Failure directions: boundary MISSING = two defects produce identical
adjacent records (ambiguity); boundary REDUNDANT = a stage's atoms are
determined by its neighbors' (noise). Operational rules, each checkable:
**R1 one-axis** — each record has a dominant atom-delta signature (rows |
col-set | values-of-named-cols | order); the emitter classifies signatures
mechanically and FLAGS multi-axis stages as split candidates. **R2
invariant ownership** — every declared invariant is checkable at ONE
boundary from that record + its predecessor; an invariant spanning 2+
stages proves a boundary missing (the signed invariant list DERIVES the
minimum partition). **R3 the fault-injection certificate** — the pack
ships a DEFECT CORPUS (leakage channels, D-V violations, program bug
history) + an injection convention; the partition test injects each defect
into a toy run and asserts trace-diff localizes to exactly the expected
stage (the null-must-die pattern applied to granularity: the partition is
atomic BY DEMONSTRATION, with a re-runnable certificate that also
regression-protects it across refactors). **R4 nondeterminism isolation** —
any rng/parallelism consumer gets its own boundary so digest divergence
pins the source.

Decision procedure: start at one boundary per audit section → split until
R2 holds → run the R3 suite, split where defects co-localize → merge
neighbor-determined stages → journal the passing suite as the partition's
certificate. Layer split: core = nothing new (diff already localizes);
pack = defect corpus + injection convention + R1–R4; program = its
partition + its certificate.

## Amendment 4 (2026-07-08, user-directed): storage ACTUALLY unified —
## emission is transport, storage is one store, identity is journaled

The Q5 section above conflated emission location with storage. Superseded:

1. **Emission = transport.** The running process writes `_trace.jsonl`
   wherever its output contract points ($HPC_RESULT_DIR / local output
   dir). A packet in flight, never a home.
2. **THE trace store (one, canonical, local, append-only):**
   `.hpc/traces/<scope_kind>/<scope_id>/...`, keyed uniformly
   {scope (run|audit), id, task, seq}. Everything INGESTS into it —
   cluster traces at harvest (one extra move on an existing pull), local
   traces at emission (zero-length hop). Transport copies are disposable
   after ingestion. There is NO fallback location: the former "fallback"
   IS the store. One reader API, one retention policy, the only place
   projections look — trace-diff needs zero knowledge of where runs
   executed.
3. **Identity = journaled sha (the receipts/dossier house pattern).**
   Trace BULK never enters the decision journal (volume would drown the
   human-boundary record; the journal's sparseness is load-bearing). At
   ingestion, ONE journaled record per trace: {scope, id, trace_sha
   (canonical hash of the ingested file), stage_count, ingested_at}. The
   journal holds the trace's fingerprint; the store holds the trace —
   tamper/regeneration breaks the sha, so traces join the trust chain
   (citable by conclusions, fingerprint-admissible, dossier-exportable)
   without journal bloat. R3 atomicity certificates are journaled records
   citing trace shas.

Same three-part shape as receipts (render file / renders dir / journaled
receipt) and dossiers (contents / store / sealed manifest) — the house
pattern, applied, which is what "a more unified way" meant.

## Coda: three lifetimes (2026-07-08 clarification)

TRACES are immutable — per-run evidence, sha-bound, valid forever as
records of their era. THE DEFECT CORPUS is the living object — append-only;
every surfaced bug distills into an injection fixture (the pipeline-v2
ruling "mechanical failures become CHECKS", given its concrete home). THE
PARTITION is versioned — it refines ONLY when a new corpus entry fails to
localize under it (the R3 suite's failure is a mechanical split
instruction), and every version journals its certificate. The system can
only get better at localizing, only in response to demonstrated failures,
never coarser, never adaptive, never rewriting history.

## Amendment 5 (2026-07-08, user-directed): storage DERIVED from consumption

Amendment 4's layout stands, but justified properly — by the consumer
table, not the house pattern: render/diff/audit-join/fingerprint/dossier
are all POINT LOOKUPS or single-key enumerations by (scope_kind, scope_id)
→ `traces/<scope_kind>/<scope_id>/` with zero indirection; every consumer
runs LOCALLY against the experiment (projections in-repo, verify local,
dossiers pack local) → the store is per-experiment `.hpc/`, never remote,
never homedir; the fingerprint joins via the SIDECAR's cmd_sha (no
trace-side index); ingestion-at-harvest exists BECAUSE the diff and
fingerprint consumers need both sides local and uniformly keyed (a diff
reaching over SSH re-imports what journal-first removed). The one
scan-shaped consumer (stage-drift-over-time) gets a DERIVED, disposable,
content-keyed INDEX when it becomes real (describe-cache / evidence-memory
ruling #4) — never a scan-optimized primary store for a consumer that does
not yet exist (the second-consumer discipline, in storage form). Retention:
arithmetic, not policy — ~1-2KB/stage ⇒ ~6MB per 200-task sweep; keep
everything forever.

## Amendment 6 (2026-07-08, user-caught omission): the COMPREHENSION consumer

The Amendment-5 consumer table was verification-only. Added first-class:
**the comprehension reader** — a human or drafting LLM reading a trace to
understand WHAT THE PIPELINE IS ("the algorithm I want to express in
code"), not whether it ran correctly. The trace as executable
documentation: generated by observation, so it cannot rot. (Run-#10
evidence: the hand-written drafting brief WAS trace content, reconstructed
manually; the drafting agent was a comprehension reader with nothing to
read.) Consequences: (1) the REFERENCE TRACE pairs with draft-context as
the dynamic half of the drafting brief — draft-context shows what the code
offers, the trace shows what the data does through it; the draft-context
doc gains it as a sibling drafting input. (2) New access pattern:
meaning-adjacent lookup ("latest trace for this profile/cmd_sha") —
resolved mechanically via sidecar keys (core agnostic; WHICH profile is
the exemplar is pack/program naming); a tiny derived lookup, storage
unchanged. (3) Renders are SELF-DESCRIBING (run identity, config identity,
atom-term column meanings in the header) — comprehension readers arrive
cold; the render's job for them is to teach. Digest-classifier logic
unaffected: comprehension consumes counts/sketches/lineage/labels, never
digests (a checksum teaches nothing; it only compares).
