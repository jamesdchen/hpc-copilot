---
status: plan
---
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
projections below are freestanding and do not wait for it. **LANDED —
Amendment 15 records the implemented shape.**

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

## Amendment 8 (2026-07-08, user-caught altitude leak): the corpus is TIERED

Amendment 7's "pack ships pre-seeded defect classes" bundled three
altitudes. Corrected: **core** seeds the generic classes for free (they are
violations of core's own invariants: conservation, label-chain break, seq
gap) + the injection INTERFACE R3 consumes. **The quant pack** ships
discipline classes AS class + atom-signature pairs (leakage channels, D-V
violations, vintage look-ahead, units-chain break — "transform-fit leakage
= a value_sketch of fitted output that changes when only future rows
change"), adoptable unedited by any quant program, plus EXAMPLE injectors
against the pack's own toy pipeline (teaching material, never
certification material). **The program** writes the RUNNABLE injectors —
each applicable class instantiated against ITS stages — plus its own
bug-history entries (the overnight 1.0-fill is harxhar's, not the
pack's). Consequences: (1) R3 certificates are earned ONLY against
program-side injectors (a pack-toy injection certifies nothing about a
real partition); (2) the tier-2 onboarding ceremony gains a step — declare
which discipline classes APPLY (inapplicable = disclosed, never silently
skipped) and instantiate injectors for the applicable ones. The bootstrap
story stands: day one = the authoring floor + class obligations from
above, arriving as classes-to-instantiate, not fixtures-to-run. (Third
same-night instance of the leak class: every noun in a pack sentence gets
the altitude test individually.)

## Implementation plan (Opus task waves — supersedes the Sequencing sketch)

Every task lands with tests that FIRE on a synthetic violation and PASS on
the happy path (docs/internals/adding-a-primitive.md). Toy fixtures only —
text/CSV frames, never a parquet, never quant vocabulary. Registry
arithmetic is RELATIVE (+2 core verbs from whatever baseline holds at
dispatch time — the slate session is concurrently adding verbs; regen
commits are STRICTLY SERIAL with theirs: run these waves between slate
phases, or on worktrees with rebake-at-merge, the proven procedure).

**Wave 0 — the gate (blocks everything):**
- **T0 outsourcing due-diligence.** Evaluate OpenLineage (+ Great
  Expectations / DVC / Hamilton adjacents) against the hard constraints
  (append-only JSONL, no daemon, stdlib-only core, sha-bindable,
  journal-native). Deliverable: an amendment to THIS DOC — a criteria
  table + adopt/refuse verdict per candidate, shapes adopted if any fit.
  Needs WebSearch/WebFetch; no repo writes beyond the doc.

**Wave 1 — the substrate (file-disjoint, dispatchable in parallel):**
- **T1 `state/data_trace.py`** — the record model (`{stage, section?,
  seq, atoms{}, flags[], trace_schema_version:1, created_at}`), the ATOM
  SCHEMA REGISTRY with per-atom comparison semantics (ONE definition —
  render, diff, and the later fingerprint interlock all consume it;
  enforcement row pins one-registry), canonical serialization (the P-S1
  helper if landed, else local + cite), the generic invariants
  (conservation / chain continuity / seq monotonicity) as pure functions,
  the store read/write (`.hpc/traces/<scope_kind>/<scope_id>/task-<n>.jsonl`,
  one file per task), and `ingest_trace(experiment_dir, scope_kind,
  scope_id, task, transport_path)` — moves the file into the store and
  journals ONE record (`block="data-trace"`, `resolved={scope, id, task,
  trace_sha, stage_count}`) via append_decision on the run/audit scope
  (the relay-due block-class precedent: absent from _BLOCK_ATTESTOR and
  receipt reductions, pinned by test). Stdlib-only (enforcement: the
  library-boundary import guard).
- **T2 the emission contract** — a constants module shared with the
  dispatcher (lock-step, like the _EXIT codes): the transport filename
  (`_trace.jsonl`), the digest env var name, the local-emission fallback
  rule (Class A reads transport directly; local runs ingest immediately).
  Plus the Class-A read helper (freshest transport copy for a draft).

**Wave 2 — the classifier + transport (serialize with anything touching
dispatch.py / the pull seam):**
- **T3 digest classifier** — a pure function over the sidecar (canary
  flag | `reproduces` set | local context | task_count ≤ threshold →
  digests on) + the dispatcher exporting the env var + the submit-spec
  override field (`trace_digests: force_on|force_off|None` — wire change:
  schema regen; exercised override recorded on the sidecar, disclosed).
  Fires-tests per context row + the degradation path (verify wanting
  digests, finding none → disclosed, never fabricated).
- **T4 ingestion-at-harvest** — the existing pull seam additionally pulls
  `_trace.jsonl` per task and calls T1's ingest. BOUNDARY FLAG: locate the
  seam at implementation (aggregate/pull path); serialize with slate
  phases touching aggregate_flow.

**Wave 3 — the projections (new files; registry +2; serial regen):**
- **T5 `trace-render`** (query verb): the four views (row waterfall w/
  conservation flags; label-chain line; feature lineage; sketch table),
  SELF-DESCRIBING header (run/config identity), deterministic markdown.
  Spec: `{scope_kind, scope_id, task?}` OR the reference lookup
  `{profile}` / `{cmd_sha}` (latest-by via sidecar join — Class B).
  Enforcement: the never-judgment grep pin (no verdict vocabulary in the
  render); trusted-display posture documented.
- **T6 `trace-diff`** (query verb): two keys → per-atom comparison via
  T1's semantics registry, FIRST-DIVERGENCE highlighted, markdown render.
  Fires-test: two synthetic traces diverging at a known stage localize
  exactly there.
- Predictable pins for both: _SPEC_VERBS, prose count, hand-filled
  docs/primitives bodies, six regen scripts.

**Deferred by design (NOT in these waves):** the fingerprint interlock
(Phase-3 amendment); the audit-view section join (canon-class view change
— lands with a canon bump between campaigns); the temporal-scan index
(waits for its consumer).

**Pack/program work (harxhar-clean, NOT core dispatch):**
- **P1** the pandas emitter draft (pack-staging): measures atoms per the
  T2 contract, stage-class vocabulary, invariant checks → flags. Can land
  BEFORE any core wave (caller-side JSONL; readable with pandas).
- **P2** instrument the executor/ridge path (the authoring-floor pass).
- **P3** discipline classes declared applicable + program injectors + the
  first R3 certificate (after P2 stabilizes a partition).

**Recorded-answer questions for the implementer** (each needs a drift-log
line, not a redesign): the journal scope for audit-context traces
(notebook scope id = audit_id — confirm against notebook journal
conventions); whether `value_sketch` quantiles are fixed (q05/q95) or
declared (recommend fixed v1); the task-file naming for single-task local
runs (task-0 — confirm no collision with audit-prelude executions).

**Acceptance for the whole feature:** a toy pipeline emits → ingests →
renders all four views → a planted divergence localizes via trace-diff →
the journaled trace_sha matches a recompute → a rootless/knob-less run
digests exactly per its sidecar context. One end-to-end contract test.

## Amendment 9 (2026-07-08, user-directed): the authoring-loop integration

**The drafting AGENT is the trace's first Class-A consumer.** The audit
prelude's inner step becomes draft → EXECUTE → read your own receipts →
fix yourself → then face the audit: the agent runs its draft locally
(Class A, fresh transport), reads the code-rendered trace (rows/flags/
labels/sketch), and corrects against FACTS instead of beliefs — the
pointing doctrine turned inward; bad drafts die at the agent's desk before
the human's sign-off. Mechanics: audit-context emissions carry `section`
(the Q3 mapping) via the emitter API; audit-scope traces land under
`traces/audit/<audit_id>/`.

**At sign-off, the view shows the diff AND the receipts** (the already-
deferred canon-change section join): each human_required section renders
its latest execution summary (rows/drops/labels/flags + the trace sha,
cited in the trusted render). The human signs "does the code look right
AND did it demonstrably do what it claims". v1 = DISPLAYED EVIDENCE, never
a gate (never-blocking: a flagged section routes to the human, nothing
auto-refuses).

**The convergence path — trace-as-receipt (later canon-change, not v1):**
the receipt machinery is the slot this grows into. Today's receipt is a
thin output sha; the section's trace is the receipt grown up (sha-bound,
section-tagged, flag-carrying). End-state: assertion-bearing sections
auto-clear only when the diff is clean AND the latest trace shows no
flags — the D-attention tier finally sees runtime evidence, and the
pack's runtime invariants join what routes human attention.

## Amendment 10 (2026-07-08, user-directed): THE OBSERVER IS THE RUNNER —
## emit ownership resolved

No code inside the run is trust-bearing. The sanctioned execution lane
(the notebook-render plugin / its lighter local runner) executes the draft
CELL BY CELL (percent format = free boundaries) and MEASURES between
cells itself: it looks up the DECLARED OBSERVABLES (the interface
contract's names — already part of what the human signs, so the signature
covers the observation plan) and takes the atoms via the pack's
measurement implementations. Ungameable by construction: the observer is
the process, not the code — a draft cannot skip a boundary, and hiding
data in undeclared names yields visibly-absent observables (a disclosure,
never a silence).

THE TRUST HIERARCHY OF EMISSION SOURCES (each honest about what it is):
1. **Runner-observed** (cell boundaries × declared names) — total coverage
   by construction; THE ONLY receipt-grade source; what sign-off surfaces
   and trace-as-receipt ever count.
2. **Engine-emitted** (the pack wraps its own engines once) — ungameable
   per-call detail, sub-cell stages; a REFINEMENT layer. Its holes stop
   being trust gaps (the runner floor covers them) and become the
   shadow-lint's runtime twin: "zero engine coverage in an executed
   section" = the section avoided the pinned engines, disclosed.
3. **Draft-emitted** (`trace.emit` in the draft) — untrusted annotation;
   Class-A self-checking convenience only; never enters receipts.

Atomicity composition: cell boundaries are the GUARANTEED observation
floor; R1–R4 partition refinement operates within it via engine emits.
Same shape as the whole system: the run does not narrate itself — trusted
code observes from outside at signed boundaries (the pointing doctrine
applied to instrumentation; the reducer/render/Stop-hook precedent).

## Amendment 11 (2026-07-08, user-directed): bar-lightening is
## CERTIFICATE-GATED, not v1-default, not indefinitely deferred

Even with runner-observed (ungameable) evidence, trace flags do not
lighten the sign-off bar at v1: (1) an untested instrument gets no gate
authority (the receipts precedent — evidence sources earn gate power
through field use); (2) "flag-free" is only as meaningful as the flag net
is dense — a clean trace under a thin invariant set means "passed a thin
check", and lightening vigilance against a thin net is the fingerprint's
n=2 lesson inverted. THE TRIGGER: the R3 certificate IS the density
measure, so **bar-lightening is a per-program entitlement gated on a
passing R3 certificate over the declared defect corpus** — no certificate,
traces are information beside the diff, full bar; certificate held,
runner-observed flag-free sections may take the lighter keystroke tier
(y-adoption of a code-drafted sign-off). Palatability becomes the REWARD
for evidence density (write your injectors, earn cheaper sign-offs), and
the entitlement stays honest: a new corpus entry that fails to localize
suspends the certificate — and the bar — until the partition refines.
INVARIANT REGARDLESS: only the KEYSTROKE cost lightens, never the ROUTING
— no trace answers a judgment section's actual question; traces may
cheapen how the human says yes, never decide they need not look.

## Amendment 12 (2026-07-08): implementation-readiness sync (waves ↔ A10/A11)

The T-waves predate Amendments 10-11; corrections making the plan
implementable as one coherent whole:

- **NEW TASK T-R (the runner, wave 2.5 — the trust-bearing half of
  emission):** the sanctioned execution lane (notebook-render plugin +
  the lighter local runner) gains the BETWEEN-CELL observation loop: after
  each cell, look up the DECLARED OBSERVABLES in the namespace and take
  the atoms via the pack measurement impls; emit runner-observed records
  (source: "runner"). Records carry their SOURCE TIER (runner | engine |
  draft) — receipts/sign-off surfaces consume runner-tier only.
- **P1 REVISED:** the pack emitter is (a) the measurement implementations
  THE RUNNER INVOKES, (b) the optional engine-wrapper refinement layer,
  (c) the Class-A convenience API. It is NO LONGER the trust-bearing
  instrumentation (A10).
- **NEEDS RULING (G-a): the observation plan's machine-readable home.**
  Candidates: (1) RECOMMENDED — the audit configuration gains
  `observables: [names]` (rides the audited_source /
  notebook-record-config seam; lands inside the signed surface
  automatically; versioned with the roots); (2) a template marker
  convention (`# hpc-trace-observe: <names>` — visible in-file, signed via
  template bytes, but a second parsing convention). Implementation blocks
  on this ruling ONLY for T-R; T0-T6 proceed regardless.
- **Recorded answers (G-c):** a `flag` is `{rule, detail, evidence{}}`
  (the notebook-lint finding shape reused — one flag vocabulary
  system-wide); ad-hoc local runs with neither run_id nor audit_id trace
  under scope ("local", <cmd_sha12>) — mechanical, collision-free; the
  REDUCE stage's trace is CORE-EMITTED counts-only (core measures its own
  pooling with stdlib; canary-exclusion counts included; no pack
  involvement, no sketch atoms there).
- **Certificate-gating (A11) adds no v1 task** — it consumes R3
  certificates and the existing tier machinery when both exist; noted so
  no implementer builds it early.

## Amendment 13 (2026-07-08): T0 outsourcing due-diligence — verdicts

Wave-0 gate discharged. The field was surveyed against the six hard
constraints; the verdict is **REFUSE every dependency, ADOPT one
vocabulary** — OpenLineage's per-column facet field NAMES as a courtesy
mapping for the atom catalog, no code, no wire, no identity model. The
four-question boundary test (`docs/internals/engineering-principles.md`
§"Library knowledge in core") is the recorded frame: none of these
libraries clears Q3/Q4 (stdlib-only, testable without the library) as a
core dependency, and none clears Q1 the other direction — OpenLineage's
`runId`-UUID identity is a *semantics* import (a second ID universe core
would have to name and reconcile), not substrate.

**(a) Criteria table** — candidate × the six constraints:

| Candidate | 1. append-only JSONL, no daemon | 2. stdlib-only core | 3. sha-bindable | 4. journal-native identity | 5. atom diff semantics | 6. per-task inline transport |
|---|---|---|---|---|---|---|
| **OpenLineage** (spec) | **partial** — client ships a `FileTransport` that appends JSON, but the STANDARD is HTTP/Kafka-to-backend; the file lane is a fallback, not the model | **fail** — `openlineage-python` pulls deps (attrs, requests); the *shape* is plain JSON (pass), the library is not | **partial** — JSON, but events carry volatile `eventTime`/`_producer`/`_schemaURL`; not canonical without stripping | **fail** — `run.runId` MUST be a UUID; `job.{namespace,name}` composite key — its own ID universe, not our run/audit scope | **fail** — facets TRANSPORT measurements; no per-atom comparison/first-divergence rules (it is a lineage-carrier, not a diff engine) | **fail** — event model is run-lifecycle (START/RUNNING/COMPLETE per *run*); a per-stage-exit record is not the unit |
| **Marquez** (OL reference impl) | **fail** — server + Postgres + REST API; a daemon by definition | **fail** — not a library, a service | n/a | **fail** | **fail** — storage/visualization, no diff | **fail** — network ingest |
| **Great Expectations** | **partial** — `ExpectationSuiteValidationResult` serializes to JSON, but emission rides a `DataContext` + stores | **fail** — heavy dep tree, not import-safe in core | **partial** — JSON, but `meta` carries `validation_time`/version | **fail** — `run_id` + `expectation_suite_name` + batch, own universe | **fail** — assertions (`success: true/false`), not diffs; and it BAKES JUDGMENT (violates our "trace shows, scientist concludes") | **fail** — validation run, not stage-exit receipt |
| **DVC** (`dvc.lock`) | **partial** — a file, but YAML not JSONL, and it is the CLI+cache's artifact, not a hand-writable record | **fail** — meaningful only with the DVC tool + object cache | **pass** — content-`md5`+`size` per dep/out (this is exactly our `digest` atom) | **fail** — keyed by `dvc.yaml` stage NAMES, no run/audit scope | **fail** — file-level "changed?" only; no `null_count`/sketch/`span`/`label_chain`; no diff engine | **fail** — repo-root pipeline lock, not per-task |
| **Hamilton** | **fail** — lineage is introspected from the in-process function DAG; no emitted record format at all | **fail** — a compute FRAMEWORK you must author inside | **fail** — no per-stage measured record to bind | **fail** — node names in a DAG | **fail** — authoring-time STRUCTURAL lineage, no runtime data measurement | **fail** — no transport; lineage lives in the running process |

**(b) Adopt/refuse verdict per candidate** (four-question form):

- **OpenLineage — REFUSE the dependency, transport, and identity model;
  ADOPT the facet field vocabulary.** Q1: its `runId`-UUID + `job.namespace/name`
  identity forces core to name and reconcile a second ID universe — a
  semantics import, refused (our identity is the journaled sha bound to
  run/audit scope, A4). Q2/Q3/Q4: the client is not stdlib and not
  import-safe on the cluster surface; the JSON *shape* is stdlib-trivial
  and needs no library, so we take the shape, not the code. The event
  model (run-lifecycle START/COMPLETE) is the wrong unit — a trace is many
  *stage-exit* records under one run, which OpenLineage can only express as
  many separate Job runs. And it carries no diff semantics (constraint 5),
  which is the actual core value here. Verdict: courtesy vocabulary only —
  see (d).
- **Marquez — REFUSE.** It is precisely the daemon+DB backend the plan
  refuses (constraints 1–2). Cited only to confirm that OpenLineage's
  *shape* is cleanly separable from its *backend*: adopting facet names
  incurs zero Marquez surface.
- **Great Expectations — REFUSE, no vocabulary taken.** Beyond the
  dependency failures (Q3/Q4), it is doctrinally opposed: its records are
  `success: true/false` assertions — judgment baked into the record — which
  violates the atom catalog's "Excluded by design: judgment fields (no
  'looks wrong')". Our `flags` are opaque-and-rendered-never-interpreted;
  GE's verdicts are the thing we deliberately keep OUT of the trace. The
  validation half of this system lives in the pack's emitter checks →
  `flags` and in R3 certificates, not in a borrowed assertion record.
- **DVC — REFUSE, no NEW vocabulary taken.** Its `dvc.lock` dep/out
  `{path, md5, size}` shape is exactly our `digest` atom + the
  data-manifest rung-0 identity — which we already have. It confirms the
  design; it adds nothing. File-level checksums cannot express the
  per-column/per-row atoms that are the point, and it is inseparable from
  the DVC tool + cache (Q3 fail).
- **Hamilton — REFUSE, doctrinally instructive.** It is not a record
  format; it is a compute framework whose lineage is the code's DAG
  structure — i.e. "what the code SAYS it does", static. That is precisely
  our AUDIT half (`docs/design/notebook-audit.md`), not the trace half.
  Hamilton reinforces the static/dynamic split this doc opens with; it is
  not a candidate for the runtime-receipt role at all.

**(c) Everything-refused rationale (the recorded paragraph future
contributors cite):** No surveyed standard is adopted as a dependency
because each fails the core constraints at the same seam — they are
*transport-and-storage* systems (ship measurements to a backend for
visualization) or *authoring frameworks*, whereas core's value is the
per-atom COMPARISON SEMANTICS and the JOURNAL-NATIVE sha identity, which
none of them carry. The record shape we need is a few dozen bytes of
stdlib JSON per stage; the expensive, opinionated parts of these projects
(UUID identity universes, daemons, object caches, assertion verdicts, DAG
runtimes) are exactly what we refuse. "Minimal-ours" wins not because the
field is immature but because our hard constraints (no daemon, stdlib
core, sha-bindable, journal-native, per-atom diff, inline per-task
transport) are a deliberately narrower target than any general lineage
standard aims at. The conformance-kit export-adapter lane (an
OpenLineage-event *emitter* over our traces, for teams that run Marquez)
remains open as a LATER courtesy, never a core dependency.

**(d) Vocabulary adopted without the dependency — the OpenLineage facet
courtesy mapping (for T1 to implement against).** T1's ATOM SCHEMA
REGISTRY SHOULD carry, per atom, an optional `openlineage_facet` note
naming the equivalent facet field, so an export adapter and cross-tool
readers get a free rosetta. Our field names stay snake_case and
scope-bound; the mapping is documentation, not a wire format:

| Our atom / field | OpenLineage facet field | Note |
|---|---|---|
| `row_count` | `OutputStatisticsOutputDatasetFacet.rowCount`; `DataQualityMetricsInputDatasetFacet.rowCount` | exact analog; OL splits input/output, we key by stage |
| `col_set` (names) | `SchemaDatasetFacet.fields[].name` (+ `.type`) | OL `fields[]` = `{name, type, description?}`; we track the name set + set-delta diff |
| `null_count[col]` | `DataQualityMetricsInputDatasetFacet.columnMetrics.<col>.nullCount` | OL's `columnMetrics` container (key = column name) is the shape we mirror for per-column atoms |
| `value_sketch[col]` | `columnMetrics.<col>.{min,max,sum,count,quantiles}` | OL has `sum`/`count` (mean derivable) + `quantiles` (object keyed by fraction, e.g. `"0.25"`, `"0.5"`); OL lacks `std`. Recommend our `value_sketch` MIRROR OL's `quantiles`-as-object-keyed-by-fraction and fix q05/q50/q95 (the A8 recorded-answer: fixed, not declared) |
| `digest` | (no per-column OL analog) | DVC `dvc.lock` out-`md5` is the nearest cross-tool spelling; ours |
| `span[col]`, `order_integrity[col]`, `label_chain`, `duration_ms`, `peak_mb` | (no OL facet) | atoms with no lineage-standard equivalent — core-original; `label_chain` (the units ledger) has no analog in any surveyed tool |

Two structural conventions worth stealing verbatim (courtesy, not
dependency): (1) OpenLineage's `columnMetrics` shape — an object whose KEY
is the column name, value is the per-column metric bundle — is the exact
layout T1 should use for `null_count`/`value_sketch` so a column's atoms
co-locate; (2) OL's self-describing `_producer` + `_schemaURL` header
convention (every facet names its producer and schema version) is the same
instinct as our `trace_schema_version:1` + self-describing render headers
(A6) — keep the version field, skip the URL. We do NOT adopt the leading-
underscore base-facet prefixing (a namespace-collision fix for a facet
registry we do not have).

Sources (plain links): OpenLineage spec
`https://github.com/OpenLineage/OpenLineage/blob/main/spec/OpenLineage.md`;
`OpenLineage.json` and the `DataQualityMetricsInputDatasetFacet`,
`OutputStatisticsOutputDatasetFacet`, `SchemaDatasetFacet`,
`ColumnLineageDatasetFacet` facet schemas under
`https://github.com/OpenLineage/OpenLineage/tree/main/spec/facets`;
facet docs `https://openlineage.io/docs/spec/facets/`; Marquez
`https://marquezproject.ai`; Great Expectations validation-result
reference
`https://docs.greatexpectations.io/docs/0.18/reference/learn/terms/validation_result/`;
DVC `dvc.lock` format
`https://dvc.org/doc/user-guide/project-structure/internal-files`;
Hamilton lineage
`https://hamilton.dagworks.io/en/latest/how-tos/use-hamilton-for-lineage/`.

Drift-log line: 2026-07-08 — T0 gate discharged; all candidates refused as
dependencies, OpenLineage facet field names adopted as a courtesy mapping
recorded in the atom registry (Waves 1–3 unblocked).

Drift-log line: 2026-07-08 — T5 `trace-render` landed (Wave 3, registry +1;
regen deferred to a serial rebake). RECORDED ANSWER on the reference lookups
(the `profile` under-specification the task flagged): `cmd_sha` resolves via
`find_run_by_cmd_sha` (the T1/runs parameter-identity join, newest-first). The
`profile` selector is IMPLEMENTED as a mechanical latest-by over the sidecar's
LITERAL `profile` field (`find_existing_runs` yields sidecars newest-first, so
the first match is the freshest exemplar) — NOT deferred, because the join is
well-defined at the core layer: the sidecar carries a `profile` key and
"latest-by-profile via sidecar keys" (A7 Class B) is exactly that scan. Core
stays agnostic to WHICH profile string is the exemplar (pack/program naming) —
the caller names it, core joins. Both reference lookups resolve to the matched
run's `("run", run_id)` trace scope. Absence (no run matched, or a resolved
scope with no recorded trace) is an honest `present=false` + `skipped` result,
never an error. The four views + the self-describing header render as
deterministic markdown carrying no verdict vocabulary (the never-judgment pin,
grep-tested over the render output).

## Amendment 14 (2026-07-09): G-a RULED — the observation plan lives in the audit configuration

**User-ruled (2026-07-09): candidate 1.** The audit configuration gains
`observables: [names]` on the audited_source / notebook-record-config seam —
inside the signed surface automatically, versioned with the roots, read by
the ONE recorded-config reader. Candidate 2 (a template marker) was rejected
on the altitude test: observable names are PROGRAM bindings (the
`endbartime` class), while templates are the shareable standard — baking
names into a template would force per-program template forks; and a second
in-file parsing convention beside `# hpc-audit-section:` is a new lint/canon
surface. Precedent: `attention_order` faced the same choice and landed in
the config. Authoring visibility is a RENDER concern (draft-context / the
audit view display the declared observables), not a storage one. T-R
unblocks.

**T-R LANDED (salvaged 2026-07-09, reviewed).** The runner between-cell
observation loop shipped: the `observables: list[str] | None` config field
rides the audited_source / notebook-record-config seam (absent → the loop is
OFF and interview.json is byte-identical — the `attention_order` precedent,
pinned by `test_audited_source_config_absent_is_byte_identical`); core's
frame-blind `stdlib_measure` + the `Measurer` protocol land in
`state/data_trace.py` (no pandas — the AST import pin holds); the plugin's
`_observe.observe_source` execs the audited source cell-by-cell, measures each
declared observable, and ingests **runner-tier** records into the audit scope
(`traces/audit/<audit_id>/`). Reviewed adversarially against A10/A12/A14; one
follow-up hardening applied over the salvaged commit: the `source` trust tier
now rides the RECORD MODEL — `make_record(..., source=)` stamps it and
`validate_record` enforces the closed T2-contract tier set
(`TRACE_SOURCE_TIERS`), replacing the plugin's external post-stamp so an
off-vocabulary tier can never enter the trust chain.

Drift-log line: 2026-07-09 — T-R salvaged from the orphaned data-trace branch,
reviewed, landed. **Section-join blockers B1 + B2 now CLOSED** (see the
"Audit-view section join — EVALUATED, STOPPED" evaluation): **B1 (no
producer)** — `observe_source` is the audit-scope trace producer that
evaluation named missing; **B2 (record model carries no `source` tier)** —
`make_record`/`validate_record` now carry and validate `source` against the
closed tier set, so a receipt/sign-off consumer has a runner-tier field to
filter on. The section join now waits **only on B3** (the per-section summary
+ freshness semantics ruling) and its payload-shape rebake. Wire/regen debt
(deferred to the serial rebake): the new `observables` field on the
`interview` `_AuditedSource` + `NotebookRecordConfigSpec`/`Result` schemas, and
the source-tier field is additive on the trace record (no `TRACE_SCHEMA_VERSION`
bump — readers tolerate the new key).

## Amendment 15 (2026-07-09): the fingerprint interlock LANDED (the Phase-3 amendment)

Implemented in `ops/verify_reproduction.py`, riding the landed
determinism-fingerprint substrate. Shape choices, each recorded:

- **Key naming**: when folded, per-stage atoms enter the compared payloads as
  `stage:<stage>.digest` and `stage:<stage>.row_count` — the `stage:` prefix
  namespaces honestly (a stage receipt, not a metric), the `.`-join matches
  the existing flatten convention (`flatten_metrics`), and the atom name is
  kept verbatim from the atom catalog. Digests are shas (str) and row counts
  ints, so both are EXACT-CLASS under the existing static classifier — no
  envelope needed, no tolerance ever applies, and they ride the SAME per-key
  sample + envelope machinery (identical→exact, differing→the
  mismatch/verdict flow). **NO new admission rule** — the existing D-consume
  admission governs the whole sample; the interlock adds keys, never policy.
- **Fold condition**: keys fold only when BOTH runs carry an ingested
  `("run", run_id)` trace (`read_trace`). One-side/neither-traced → NOTHING
  folded and the presence DISCLOSED on the v2 receipt's `stage_interlock`
  block (`{original_trace_present, repro_trace_present, compared,
  stage_keys}`) — the digest-policy degradation posture (disclosed, never
  fabricated, never blocking). A fully untraced pair emits a receipt
  BYTE-IDENTICAL to a pre-interlock one (pinned by test).
- **Stage-localized mismatch**: on a routed verdict (mismatch / needs_verdict
  / incomparable) of a both-traced pair, the FIRST diverging stage by
  pipeline order (the trace's `seq`; min across sides for shared stages; a
  one-side-only stage counts as divergence) surfaces as the machine field
  `diverged_stage` on the receipt AND the result, and is appended to the
  code-rendered `reason` ("diverges at stage 'scaling'") — never
  prose-invented. Null on match/auto_cleared.
- **Recorded scope answers**: v1 reads task-0 of the run scope only
  (multi-task trace enumeration is a deferred refinement); a PARTIAL
  reproduction skips the interlock entirely (it already namespaces per task —
  folding whole-run stage keys under a subset comparison would be dishonest);
  a stage seen twice keeps its LAST record (append order); a stage missing a
  digest still folds its row_count (an off-digest-policy run contributes
  counts); a digest recorded on only ONE side of a shared stage is a degraded
  observation, NOT a divergence.
- **Wire debt (regen deferred)**: `ReproductionReceipt` gains
  `stage_interlock` + `diverged_stage` (optional, default-absent — v1/v2
  pre-interlock lines parse unchanged); `VerifyReproductionResult` gains
  `diverged_stage`. Schema regen NOT run here (serial-regen discipline) —
  rebake at merge.

## Drift-log evaluations (2026-07-09): the two deferred-by-design leftovers

Both items from the "Deferred by design" list were re-evaluated in the
run-#11→#12 between-campaigns window. One stays deferred pending rulings; one
stays deferred for want of a consumer. NO code changed and NO view_sha moved —
recorded here so the next session does not re-derive.

**(1) Audit-view section join — EVALUATED, STOPPED. AMENDED same day: B1 and
B2 below are CLOSED by the T-R salvage (see the T-R drift-log entry above —
the runner now produces audit-scope runner-tier records and the `source` tier
rides the record model, `validate_record`-enforced). The join now waits ONLY
on the B3 ruling.**
The recorded intent (Amendment 9: each `human_required` section renders "its
latest execution summary — rows/drops/labels/flags + the trace sha, cited in
the trusted render") CANNOT be implemented now without inventing join
semantics. Three independent hard blockers, each sufficient on its own:

- **B1 — no producer.** Nothing emits audit-scope traces
  (`.hpc/traces/audit/<audit_id>/`). The only trace producer in the tree is
  the run-scope harvest ingest (`ops/aggregate_flow.py`). The T-R runner (the
  notebook-render plugin's between-cell observation loop, A10/A12) that would
  observe cell boundaries × declared observables and emit `source:runner`
  records is NOT built (G-a was ruled in A14 "T-R unblocks", but the runner
  itself was never implemented). A section join today renders EMPTY for every
  real audit — the dead-display class.
- **B2 — the record model carries no `source` tier.** A10 is doctrine:
  "receipts/sign-off surfaces consume runner-tier only … draft-emitted never
  enters receipts." The tier vocabulary exists ONLY in the T2 contract
  (`execution/mapreduce/data_trace_contract.py`: `TRACE_SOURCE_{RUNNER,ENGINE,
  DRAFT}`, `RECEIPT_GRADE_SOURCES`) and is consumed by nothing.
  `state/data_trace.py` `make_record`/`validate_record` neither carry nor
  validate a `source` field. A join that reads `read_trace(…,"audit",
  audit_id,…)` and renders records unfiltered would put untrusted (draft/
  engine) evidence into the SIGNED view — a direct A10 violation. There is no
  runner-tier filter to apply because the field is not on the record.
- **B3 — the per-section summary semantics are unspecified.** "One section :
  many stages" (atom catalog). A9 names the fields to show but not: which of a
  section's many stages supplies `rows/drops` (first / last / net
  conservation?); what "the trace sha" means at SECTION granularity (sha of
  the section's record subset, or the task's whole journaled `trace_sha`?);
  and there is NO section-level freshness binding — trace records carry a
  `section` slug but not a `section_sha`, so (unlike render receipts, which
  bind `section_sha` and refuse drift in `_assertions_green`) a stale trace
  would render as if current in a trusted view. Choosing any of these is
  inventing semantics, which this task forbids.

Framing correction for the implementer: the task presumed "a versioned
canonicalization constant — find how the last bump was done." There is NONE in
the audit-view path. `view_sha` is a pure content hash (`_sha_json` over the
payload dict in `ops/notebook/audit_view.py`); the two prior payload-shape
changes (T12 `attention_order`; the full-view-recompute) added payload FIELDS
and rebaked fixtures with NO integer version bump. `TRACE_SCHEMA_VERSION`
(`state/data_trace.py`) versions the TRACE record, not the audit view, and is
"bump only on a breaking record-shape change." So there is no canon-version
seam to turn — the join, when it lands, is a payload-shape change + fixture
rebake, gated behind the three rulings below.

RULINGS NEEDED before this can land (each a drift-log answer, not a redesign):
(a) does the section join wait on T-R + the `source` tier landing on the
record model (recommended — otherwise it renders dead/untrusted evidence), or
is a v1 that reads whatever exists acceptable?; (b) the per-section summary
reduction (which stage supplies rows/drops; the section-level trace-sha
definition); (c) section-level freshness — bind a `section_sha` onto
audit-scope records (mirroring receipts) so a drifted trace is refused, or
render unbound with a disclosed "as-of" stamp.

**(2) Temporal-scan index — EVALUATED, no consumer, stays deferred (correct
by the doc's own gate).** Swept the tree for a would-be consumer of the
"stage-drift-over-time / many runs" scan (the only scan-shaped consumer in the
table). The COMPLETE trace-store consumer set is: `trace-render` (point lookup
+ Class-B latest-by-reference), `trace-diff` (two point lookups),
`verify_reproduction` (run-scope task-0), and `aggregate_flow` (ingest). NONE
walks many runs' traces linearly to compute drift over time. Per the storage
ruling ("a DERIVED, disposable, content-keyed index when it becomes real —
never a scan-optimized store for a consumer that does not yet exist") the index
is NOT built. An index with no consumer is the dead-code class; it stays
deferred until a stage-drift consumer is authored.
