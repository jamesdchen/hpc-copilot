# The data trace — stage receipts for the pipeline (the audit's runtime twin)

**Status: PLANNED, USER-RULED (2026-07-08; consolidated from six amendments
— the drift log records the evolution).** The product one-liner applied
WITHIN a run: "what changed between stage N and N+1, answered mechanically
instead of by archaeology." Motivating incidents are run #10's, same night:
the 0.13242-vs-0.120 window question answered by agent narration;
246,059→218,905 row accounting reconstructed from memory; the arm-alignment
inner-join eyeballed; the canary-exclusion count a standing manual
watch-item across three runs; the hand-written drafting brief that was
trace content reconstructed by hand.

## Design center

The audit template's sections answer "what does the code SAY it does"
(static, signed). The trace answers "what did the data actually DO"
(dynamic, per-run) — stage receipts emitted INLINE at stage exit, never
reconstructed post-hoc (a reconstructed trace is a story again; an inline
trace is evidence: transient intermediates, identity binding to the run
that produced the number, cheap-at-the-moment counts).

## The atom catalog (what core owns)

An ATOM is a named, typed, meaning-free measurement of tabular data flow —
and each atom carries its COMPARISON SEMANTICS, which is what makes the
diff engine discipline-generic:

| Atom | Measures | Diff semantics |
|---|---|---|
| `row_count` | rows at stage exit + declared drops | exact; feeds the conservation invariant |
| `col_set` | column names present | set-delta (added/dropped) |
| `null_count[col]` | missing per column | exact per key |
| `value_sketch[col]` | mean/std/quantiles/min/max | tolerance |
| `span[col]` | first/last of an ordered column | exact endpoints |
| `order_integrity[col]` | monotonic? dups? gaps vs a DECLARED grid | exact |
| `label_chain` | an opaque tracked label per stage | equality along the chain |
| `digest` | content sha | exact |
| `duration_ms`, `peak_mb` | cost | tolerance |

Record container: `{stage, section?, seq, atoms{}, flags[], created_at}`.
`stage` is the fine-grained emit; `section` optionally names the audit
slug housing it (one section : many stages). Both opaque to core.

- **`label_chain` is the units ledger, generalized**: core knows "a label
  the caller tracks through stages and wants unbroken", never "units". The
  quant pack declares one instance named `units_space` (+ the
  claimed-units doctrine); the program supplies the labels (raw-var /
  sqrt / winsorized / smeared-raw). The #1 historical mirage class
  (units/target inversions) reads as a broken link in the render.
- **Generic invariants only in core** — pure arithmetic over atoms with
  zero meaning: row conservation (`rows_out == rows_in - dropped`,
  violations flagged loudly), label-chain continuity, seq monotonicity.
  Deliberately NO invariant DSL: core never evaluates pack-authored
  expressions. Pack/program invariants are checked IN THE EMITTER and
  recorded as opaque `flags` core renders but never interprets.
- **The measurement protocol**: core defines each atom's input contract
  (shape-validated values); the pack's pandas-aware emitter is the
  implementation that measures frames and emits. Core validates shapes,
  never touches frames (the receipts seam: caller executes, core binds).
- **Excluded by design**: data VALUES (timestamps at head/tail only — a
  trace is shareable evidence, never a data leak) and judgment fields (no
  "looks wrong": the trace shows, the scientist concludes — the pointing
  doctrine applied to data).

**Stage zero = input identity**: the data-manifest shas (rung 0), closing
the chain end-to-end: which bytes in, what happened at every step, what
number out.

## Stage granularity: atomicity DEFINED (testable, not aesthetic)

Atomicity is a TWO-ARGUMENT property — relative to (a) the atom catalog
and (b) a DECLARED DEFECT SET (the bug classes the trace must localize):

> A partition is LOCALIZATION-COMPLETE when every declared defect, if
> present, first alters the atoms of exactly one stage's record; MINIMAL
> when no boundary can be removed without merging two defects into one
> stage. An ATOMIC STAGE is an element of a minimal localization-complete
> partition.

Failure directions: boundary MISSING = two defects produce identical
adjacent records (ambiguity); boundary REDUNDANT = a stage's atoms are
determined by its neighbors' (noise). Operational rules, each checkable:

- **R1 one-axis** — each record has a dominant atom-delta signature
  (rows | col-set | values-of-named-cols | order); the emitter classifies
  signatures mechanically and FLAGS multi-axis stages as split candidates.
- **R2 invariant ownership** — every declared invariant is checkable at
  ONE boundary from that record + its predecessor; an invariant spanning
  2+ stages proves a boundary missing (the signed invariant list DERIVES
  the minimum partition).
- **R3 the fault-injection certificate** — the pack ships a DEFECT CORPUS
  (leakage channels, D-V violations, program bug history) + an injection
  convention; the partition test injects each defect into a toy run and
  asserts trace-diff localizes to exactly the expected stage. The
  partition is atomic BY DEMONSTRATION, with a re-runnable certificate
  that regression-protects it across refactors (the null-must-die pattern
  applied to granularity).
- **R4 nondeterminism isolation** — any rng/parallelism consumer gets its
  own boundary so digest divergence pins the source.

Decision procedure: one boundary per audit section → split until R2 holds
→ run the R3 suite, split where defects co-localize → merge
neighbor-determined stages → journal the passing suite as the partition's
certificate.

## When it is captured (all execution contexts)

At STAGE EXIT, inline, in: the local gauntlet ("did my cheap-kill see what
I think?"), the canary (trace-diff canary-vs-local catches deploy/data
divergence in one glance), every array task (arm-keyed — "did both arms
see identical rows?"), and THE REDUCE/AGGREGATE STEP — pooling and
canary-exclusion are data transformations too; "exactly N rows
canary-excluded" becomes a trace record instead of a per-run eyeball item.

## Digest policy: NO KNOB — the classifier decides

Digests (full-frame content hashes) are the only expensive atom, and they
have exactly one consumer class: identity questions (reproduction
verification, canary-vs-local, fingerprint admission). Whether a run IS
one of those is recorded before it starts — the canary flag, the sidecar's
`reproduces` field, local-gauntlet context, `task_count`. The DISPATCHER
reads the sidecar and exports the digest flag into the task env: code sets
it, the human never sees a decision point. Counts/sketches are always on
(~free next to a walk-forward).

FAILURE POSTURE (what makes knob-removal safe, not just convenient):
on-when-unneeded = bounded seconds wasted; off-when-needed = verification
DEGRADES to whole-run comparison and DISCLOSES "stage digests unrecorded"
— the status quo plus honesty, never a block, never a fabricated match. A
spec-level override exists (force_on/force_off) but is an OVERRIDE, never
a prompt, and its exercise is disclosed. The classifier's mapping is
human-owned frozen code — changing the CLASS is a reviewed edit; instances
never ask; nothing adapts. (The house pattern's third instance: auto-clear
tiers, tiered verdicts, digest policy — the run's recorded identity
determines its observation level.)

## The fingerprint interlock (lands with Phase 3)

Stage digests are fingerprint-admissible evidence from day one: the
envelope accrues per-stage, and a reproduction mismatch localizes to a
NAMED STAGE ("diverges at scaling") instead of "the runs differ". A
Phase-3 amendment (the sample-admission model gains per-stage keys); the
projections below are freestanding and do not wait for it.

## Consumers (both kinds) and projections

| Consumer | Reads | Access pattern |
|---|---|---|
| Trace render | one run's trace | point lookup (scope, id) |
| Trace diff | two traces | two point lookups |
| Audit-view join | one audit's stages by section | point lookup, scope=audit |
| Fingerprint admission | a run's stage digests + lineage | point lookup; joins via the SIDECAR's cmd_sha |
| Dossier export | all traces of a run, sha-manifested | enumeration under one key |
| R3 certificates | toy-run emissions | ephemeral, test-scope |
| **Comprehension reader** | a REFERENCE trace, to learn what the pipeline IS | meaning-adjacent lookup: "latest trace for this profile/cmd_sha" |
| Stage-drift-over-time | many runs | temporal scan → a DERIVED index, later |

**The comprehension reader is first-class**: a human or drafting LLM
reading a trace to understand the algorithm they want to express in code —
executable documentation, generated by observation, so it cannot rot. The
REFERENCE TRACE pairs with draft-context as the dynamic half of the
drafting brief (draft-context = what the code offers; the trace = what the
data does through it). Its lookup rides sidecar keys (core agnostic; WHICH
profile is the exemplar is pack/program naming). Comprehension never
consumes digests — a checksum teaches nothing; it only compares.

**Projections** (all code-rendered, deterministic, trusted-display class —
the LLM points; SELF-DESCRIBING headers with run/config identity, because
comprehension readers arrive cold):

1. **Row waterfall** — stages × counts, conservation-checked.
2. **Label-chain line** — e.g. the units round-trip, breaks visible.
3. **Feature lineage** — column → birth stage.
4. **Sketch table** — per-stage distribution of declared columns (how you
   SEE winsorization bite / a scale guard fail to fire).
5. **Trace diff** — two runs overlaid; the FIRST stage where any atom's
   comparison diverges is highlighted. Canary-vs-local, arm-vs-arm,
   today-vs-last-known-good.

Pull-only; NO alarms — the trace feeds briefs/verdicts only through the
existing surfaces (D8: route only what blocks).

## Storage: emission is transport; storage is ONE store; identity is journaled

Derived from the consumer table, not asserted:

1. **Emission = transport.** The running process writes `_trace.jsonl`
   wherever its output contract points ($HPC_RESULT_DIR / local output
   dir). A packet in flight, never a home.
2. **THE trace store** — one canonical, local, append-only store:
   `.hpc/traces/<scope_kind>/<scope_id>/...`, keyed {scope (run|audit),
   id, task, seq}. Everything INGESTS into it — cluster traces at harvest
   (one extra move on an existing pull), local traces at emission
   (zero-length hop); transport copies disposable after ingestion. No
   fallback location exists: this IS the store. Point-lookup layout
   because five of seven consumers are point lookups; LOCAL placement
   because every consumer runs locally against the experiment; ingestion
   exists BECAUSE diff/fingerprint need both sides local and uniformly
   keyed. The one scan-shaped consumer gets a DERIVED, disposable,
   content-keyed index when it becomes real — never a scan-optimized
   primary store for a consumer that does not yet exist. Retention:
   arithmetic, not policy (~1-2KB/stage ⇒ ~6MB per 200-task sweep; keep
   everything).
3. **Identity = journaled sha.** Trace BULK never enters the decision
   journal (volume would drown the human-boundary record). At ingestion,
   ONE journaled record per trace: {scope, id, trace_sha, stage_count,
   ingested_at}. Tamper/regeneration breaks the sha — traces join the
   trust chain (citable by conclusions, fingerprint-admissible,
   dossier-exportable) without journal bloat. R3 certificates are
   journaled records citing trace shas. (The house three-part shape:
   receipts = render file / renders dir / journaled receipt; dossiers =
   contents / store / manifest; traces = transport / store / journaled
   sha.)

## Outsourcing due-diligence (adopt-if-better — FIRST TASK)

Others likely do parts of this better; the plan's first task is a gate,
not a formality (the filelock/psutil precedent): evaluate **OpenLineage**
(record/facet shapes; column-level lineage) and the adjacent field (Great
Expectations — the validation half; DVC / Hamilton / dagster — asset
lineage) against the HARD constraints: append-only JSONL, no daemon,
stdlib-only core (pandas lives pack-side), sha-bindable records,
journal-native. IF a standard's record shape fits, ADOPT THE SHAPE ITSELF
(vocabulary and facets included); if not, minimal-ours + an export adapter
in the conformance-kit lane, with refusal reasons recorded here. The
verdict amends this doc before implementation starts.

## Layer split (atoms / composition / binding)

| Layer | Owns |
|---|---|
| Core | the atom catalog + comparison semantics, the record container, generic invariants, the measurement protocol, the store + ingestion + journaled shas, the render/diff engines. Stdlib-only. |
| Quant pack | the pandas-aware EMITTER, stage-CLASS vocabulary (load/transform/feature/split/fit/score — quant-general, never a program's stage names), which atoms per class, the `units_space` label-chain instance, class-altitude invariants (parameterized, never h=1), the DEFECT CORPUS + injection convention + R1–R4 |
| Program | concrete stages bound to classes, actual labels and parameters, program invariants as emitter checks → flags, its PARTITION + its journaled certificate |

The altitude test both ways governs every future atom/composition: "would
a second program adopt it unedited?" (binding→pack leak) and "would a
second DISCIPLINE adopt it unedited?" (pack→core leak). An atom failing
the second test belongs in a pack, not core.

## Three lifetimes

TRACES are immutable — per-run evidence, sha-bound, valid forever as
records of their era. THE DEFECT CORPUS is the living object — append-only;
every surfaced bug distills into an injection fixture (the pipeline-v2
ruling "mechanical failures become CHECKS", given its concrete home). THE
PARTITION is versioned — it refines ONLY when a new corpus entry fails to
localize under it (an R3 failure is a mechanical split instruction), and
every version journals its certificate. The system can only get better at
localizing, only in response to demonstrated failures, never coarser,
never adaptive, never rewriting history.

## Sequencing (the cheapest adoption curve)

1. **The outsourcing gate** — amends this doc.
2. **Instrument harxhar with a pack-side emitter draft** — caller-side
   JSONL needs ZERO core code; files immediately readable with pandas; the
   record shape is proven against reality before core freezes it.
3. **Core store + ingestion + projections** (render + diff) land on
   already-flowing data; registry +1 or +2.
4. **The fingerprint interlock** as a Phase-3 amendment; the
   draft-context doc gains the reference trace as a sibling drafting
   input.

Enforcement: toy fixtures only in core tests; the never-judgment pin (the
render contains no verdict vocabulary — grep-testable); the pointing
doctrine (renders relayed verbatim); the never-blocking posture (no trace
condition ever gates a run).

## Drift log

- 2026-07-08: first draft (five rulings: fingerprint interlock; no-knob
  digests; stage-finer-than-section; adopt-if-better gate; storage
  unification).
- 2026-07-08, same session: six user-driven amendments folded and the doc
  CONSOLIDATED (superseded body text removed): A1 the atom catalog (core
  atoms + comparison semantics; packs compose; programs bind; no invariant
  DSL — the units ledger generalized to `label_chain`); A2 the digest
  classifier mechanics (sidecar-derived, dispatcher-exported,
  safe-degradation, override-never-prompt); A3 atomicity DEFINED
  (localization-complete minimal partitions, fault-injection
  certificates), superseding "convention"; A4 storage = transport / one
  ingesting store / journaled sha; A5 storage DERIVED from the consumer
  table (point-lookup layout, local placement, index-not-store); A6 the
  comprehension consumer (reference traces as the drafting brief's dynamic
  half; self-describing renders). Coda: three lifetimes.

## Amendment 7 (2026-07-08, user-directed): the bootstrap, the consumer
## classes, and the drafting brief

**Bootstrap (the chicken-and-egg dissolved):** the partition is NOT born
from bugs. (a) The corpus is pre-seeded by the PACK (leakage channels, D-V
violations, discipline failure modes) — program bug history refines it,
never creates it. (b) The day-one consumer is THE AUTHOR: the initial
granularity comes from THE AUTHORING FLOOR — emit a stage wherever you
write a transform you'd explain as one step; emission is part of the
authoring act (write the call, write its emit, run, watch the waterfall
row appear — live feedback while building). Authoring granularity
naturally satisfies R1; R3 refinement under the growing corpus splits
where real bugs smear. Collection therefore starts at THE FIRST
EXPLORATORY EXECUTION, not after bugs exist.

**Consumer CLASSES (the organizing principle — each class fixes a
freshness/lookup/render contract):**
- **A. AUTHORING** (comprehension-own): the builder mid-creation; lookup =
  "my draft's latest execution"; freshness = per cell-run, PRE-ingestion —
  the ONE consumer allowed to read transport copies directly; render =
  live terse waterfall.
- **B. REFERENCE** (comprehension-others): the drafting brief, onboarding;
  lookup = meaning-adjacent (latest-by-profile via sidecar keys);
  freshness = lagging OK; render = self-describing, teaching-shaped.
- **C. VERIFICATION/IDENTITY**: diff, fingerprint, audit-join, dossier,
  R3 certificates; exact keys, POST-ingestion only, sha-bound comparison
  renders. (Why ingestion + journaled shas exist.)
A future consumer is CLASSIFIED FIRST; its class dictates its contract.

**The drafting brief (planned composition):** draft-context render
(static: what the code offers) + reference trace render (dynamic: what the
data does through it), code-composed into ONE artifact the drafting step
reads — the run-#10 hand-rolled brief, fully mechanized. Lands as a small
follow-up on the built draft-context (its skill step gains the second
input) + this pairing note.
