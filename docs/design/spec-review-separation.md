---
status: plan
---
# Spec review — separating syntax from logic at the human boundary

**Status:** DRAFT — a design plan in the human-amplification line. Extends the
propose→`y`/nudge loop with a concern-separation: the human reviews *logic*, code
owns *syntax*.
**Date:** 2026-07-03
**Parent:** [`human-amplification-blocks.md`](human-amplification-blocks.md)
(extends §1's division of labor and §2's propose loop);
sibling of [`block-drive.md`](block-drive.md).

---

## 1. Principle — the human reviews logic, never syntax

The guiding doctrine (§1) already says: **code does all mechanical digestion; the
human makes every decision.** This document applies that one layer deeper — to
the *spec-review surface* itself. A spec has two kinds of "wrong," and only one is
a human decision:

- **Syntax** — does the spec validate (shape, types, nesting, cross-field
  mechanical constraints)? This is mechanical digestion → **code's job.** The
  human should never be shown, or asked to fix, a syntactically-invalid spec.
- **Logic** — is this the *right* experiment (cluster, grid, budget, the research
  intent)? This is irreducible judgment → **the human's job**, and the *only*
  thing the propose loop surfaces for `y`/nudge.

The LLM's role stays exactly what §1 assigns it: a translator. It never authors
the spec (where it fumbles syntax) and never decides (where §1 forbids it). It
digests the human's intent into inputs and renders code-built evidence back out.

### The three layers of "wrong"

"Syntax" hides three distinct things; the design is really about drawing one line
(L2/L3) correctly.

| Layer | What | Who resolves it | Human sees it? |
|---|---|---|---|
| **L1 — shape/type** | pydantic `model_validate` (nesting, types, required, `extra="forbid"`) | code (by construction — §2) | never |
| **L2 — mechanical-semantic** | walltime>0, gpu/partition compat, qos limits, dataset present — the `validate-*` primitives | code | **only when it's a genuine conflict/choice** |
| **L3 — logic/intent** | is this the right experiment? | **the human** | always — this *is* the brief |

**Scope.** This document covers **spec-construction** decisions (a proposed spec →
`y`/nudge). The propose loop (guiding §2) has two other decision types — a
**debugging fix** on a failed block, and **interpretation of results**. A fix is
partly code-draftable and inherits the faithful-render and code-enumerated-choice
invariants here; *interpretation of results is not a spec at all* — the
no-fabrication doctrine (#355) applies but the intent→build machinery does not.
Those two get their own treatment; do not stretch this design over them.

## 2. Intent in, spec out — the LLM never emits a spec

The latency worry ("re-draft against the validator until green") is the tell that
an LLM↔validator loop is the wrong shape. **The fix is to eliminate the loop, not
speed it up:** the LLM never authors schema-shaped output at all.

**The LLM emits a flat *intent bag*; code builds the validated spec.** The intent
bag is the small set of human-judgment fields as forgiving key-values
(`cluster=hoffman2, grid=50, walltime=hold`) — no nesting, no schema shape. A code
builder assembles the correctly-nested, validated spec.

Every field in the spec has **exactly one deterministic origin**, and the LLM
touches only one of them (and only its *value*, never its *shape*):

| Origin | What | Filled by | Example |
|---|---|---|---|
| **Intent** | the human-judgment fields | LLM writes the value into a schema-declared slot | `cluster`, `grid` |
| **Block-invariant** | fields the block requires by definition | code constant in the builder | `canary=True` for s2 |
| **Auto-resolved** | anything with a safe default | the resolution layer (§4) | `partition`, `mpi_pe` |
| **Derived** | computed from the above | pure function, never LLM | `cost_estimate`, `cmd_sha` |

Because the LLM never touches shape, **L1 is structurally impossible to get
wrong**, and the re-draft loop for the entire "syntax" class disappears. Residual
LLM round-trips reduce to exactly two, both acceptable: one generation to draft
the intent bag (cold start, a single call — not a loop), and one per nudge to fold
the human's "halve the grid" into intent edits (latency the human is already
waiting on, because they just typed).

### 2a. Structured intent rides the existing typed-tool surface

**Decided:** the intent bag is the arguments to a **typed tool** (the block's own
`input_schema`, already projected as an MCP tool by `mcp_server.py`), *not* a
raw-API structured-output call.

- In the human-amplification fork the LLM in the loop is the **interactive
  session itself** — there is no execution-layer LLM (that is why the #137 OAuth
  blocker dissolves). So "structured output" here is the harness's native
  tool-call validation against `input_schema` — free under the subscription, no
  activation, no metered credits, no API key.
- The raw-API forms (`output_config.format`, `strict: true`) *do* work under
  subscription OAuth — but using them means **the fork makes its own Anthropic API
  call**, which reintroduces exactly the worker + invoker-auth surface the fork
  deleted. **Ruled out.** If anyone reaches for `output_config.format`, that is the
  signal they are about to re-open the #137 door.
- Harness tool-calling gives **validate-and-retry**, not grammar-constrained
  decode — weaker than `strict:true`, but it does not matter: the LLM emits flat
  *values*, code builds the shape, and pydantic at the builder is the terminal L1
  guarantee.

### 2b. No coercion stage — normalization lives in the builder

An earlier sketch had a "deterministic coercion layer" before any re-draft. It is
**unnecessary** once code builds the spec — it was a vestige of the
LLM-authors-the-whole-spec model. Value-normalization *is* part of building:

- **Interpretable-but-nonstandard** (`walltime="1h"`, `gpu="V100"`, `grid="50"`)
  → the builder parses it (unit→seconds, lowercase enum, str→int). Zero latency,
  zero LLM calls, no separate stage.
- **Genuinely uninterpretable** (`grid="big"`) → the intent tool's schema rejects
  it → a *rare* re-draft. No coercion could have saved it.

The one guardrail: the builder normalizes only **unambiguous** representations;
anything ambiguous escalates. This is safe **because** every normalized/defaulted
value shows up in the faithful render (§6) as `walltime: 3600s (from "1h")` — a
misparse is visible at the exact review step that exists to catch it. Leniency is
never silent.

## 3. The builder is two layers — effectful *resolution*, pure *assembly*

Spec construction must not be diffuse (its diffuseness is what let the wave-4
`block-drive` nesting bug live in two places — the driver *and* the block hints).
But "the builder" is **not one pure function** — it splits into two layers with
different contracts, and conflating them (an earlier draft called the whole thing
"pure") is a mistake, because resolution does SSH:

```
resolution (effectful) : (intent, cluster) → resolution_artifact   [SSH, file reads; cached §4]
assembly   (pure)      : (intent, artifact) → (spec, provenance)    [deterministic, no I/O]
```

- **Resolution is effectful** — it reads `clusters.yaml`, run priors, and probes
  the cluster over SSH (preflight's `resolve-resources`). It produces the
  content-addressed *resolution artifact* (§4); this is where the I/O lives.
- **Assembly is pure** — given the intent bag and a resolution artifact, it emits
  `(spec, provenance)` deterministically: places each intent value, applies
  block-invariants and defaults from the artifact, computes derived fields. No I/O.
  **"The builder is pure" is true only of this half** — every purity argument in
  this doc (the tier guards a function; scaffold dissolves, §4c) is about
  *assembly*, not resolution.
- **One door for callers.** The driver and intent tool call `build(intent)`, which
  runs resolution (a cache-hit when the artifact exists — §4) then assembly; the
  resolution primitives stay composed *inside* the resolution layer.
  `build-submit-spec` is the seed; give it status/aggregate/campaign siblings.
- **Provenance is the unlock, not tidiness.** A first-class builder tags every
  field with its origin (`human` / `invariant` / `default` / `derived`) as it
  assembles. The scattered pipeline cannot emit unified provenance; a single
  builder can — and that tag is exactly what the faithful render (§6) needs to
  show *chosen* vs *defaulted*. So **first-classing the builder is a dependency of
  §6, not a nice-to-have.** (`provenance-manifest` already exists — per-field
  origin is the same doctrine one level finer.)
- **The tier guards a real signature.** The integration tier's spec-contract layer
  pins `build(intent) ⊨ model`; today that guards a phantom pipeline, after this
  it guards an actual function.

## 4. The resolution layer — shared infrastructure, one artifact

**Decided (evidence-grounded).** The builder does **not** subsume everything. A
consumer-grep (2026-07-03) shows the resolution primitives have real consumers the
new design *keeps*, so they are a shared layer *below* the builder, not the
builder's private internals:

```
                    ┌── human builder   (intent + provenance + absorbed defaults/walk)
resolution layer ───┼── campaign spine  (strategy inputs per tick, no human)
(resolve-resources, │
 resolve-submit-    └── preflight        (resolve-to-validate)
 inputs)
```

- `deterministic_resolver.py:485` (campaign) and `submit_preflight.py:95/215`
  (preflight) are non-human consumers that survive → resolution stays a shared
  layer. The builder is **one consumer, not the owner.**
- The **artifact** must be shared too, or the layers desync. Generalize to a
  **content-addressed resolution artifact**, keyed like the canary cache
  (`sha(resource_intent ⊕ cluster_config_sha ⊕ cluster_state_epoch)`, with a TTL):

| Consumer | Reuse | Invalidation |
|---|---|---|
| Campaign spine | **already optimal** — the resolved `resources` block is frozen in the run sidecar and carried forward; the per-tick `resolve-submit-inputs` recomputes only `run_id`/`cmd_sha` (`deterministic_resolver.py:470-480`). This is the §4 greenlit-freeze, more correct than a cache (reproducible). | never re-resolve mid-flight (hard freeze, epoch pinned at greenlight) |
| Preflight → build | **gap** (see §4a) | short TTL / same-flow hand-off |
| Human cold start | first resolution, nothing to reuse | n/a |

The campaign's sidecar-freeze is the *hard* special case of the content-addressed
artifact; preflight→build is the *soft* case. Both drop out of the first-class
builder for free: if `build(intent)` is content-addressed on its inputs, all three
consumers dedupe against one artifact, and the tier guards one cache instead of
three ad-hoc ones.

### 4a. Preflight→build coherence (verified gap)

`submit-s1` composes `submit-preflight → walk-submit-ambiguities →
resolve-submit-inputs` (`submit_blocks.py:228`). Preflight runs `resolve-resources`
**with SSH** — the expensive cluster probe (`submit_preflight.py:208-214`).
`resolve-submit-inputs` is `requires_ssh=False` (`resolve_submit_inputs.py:140`).
In `submit_s1`, preflight's result is summarized into `brief["preflight"]`
(`submit_blocks.py:264`) but the build calls `resolve_submit_inputs(...,
spec=spec.resolve)` (`:304`) — it does **not** consume preflight's resolved block.

**So the cost is not a double SSH round-trip** (the probe runs once). It is that
**preflight validates one resolved block and the build constructs from another
path** — validate A, submit B. That is the exact hazard §1's L2 layer exists to
prevent (validators are meaningless if they validated something the builder did
not produce). The fix: thread preflight's resolution artifact into the build (or
have both hit the shared content-addressed cache), so *what is validated is what is
built*. This is a **coherence** fix, not a latency one — which is the stronger
reason to make it.

### 4b. What the builder subsumes (evidence-grounded)

The test is **not** "used elsewhere today" (circular — current usage is the old
design defending itself). It is: *a stage survives as a peer only if folding it in
would violate **assembly's** contract (pure, no I/O — §3), or if it has a
consumer the new design does not also replace.* Re-run over the grep:

| Stage | Real surviving consumer? | Verdict |
|---|---|---|
| `apply-safe-defaults` | no (old worker flow + LLM prompt) | **fold into builder** (its default origin) |
| `walk-submit-ambiguities` | no (old worker) | **fold in** — it *is* provenance + validator outcomes |
| `resolve-submit-inputs` | **yes** (campaign spine) | **shared resolution layer** |
| `resolve-resources` | **yes** (campaign + preflight) | **shared resolution layer** |
| `scaffold-spec` | duplicated by the builder (`submit_spec.py:247` "mirroring scaffold_spec's coherent-pair discipline") | **dissolve** (see §4c) |

### 4c. Scaffold dissolves — no scaffold-specific side-effect

Scaffold's apparent separateness was a disk-I/O side-effect. But the builder
already *duplicates* its coherent-pair discipline (`submit_spec.py:247`). The clean
subsumption removes both the side-effect justification and the duplication:

- Split the **pure** part (produce the spec value + executor-template **as data**)
  from the **effect** (write files).
- The builder emits `(spec, provenance, scaffold_artifacts?)` — all data, still
  pure, no I/O.
- **One generic `materialize` effect** writes whatever artifacts came out — the
  same writer that already persists `interview.json` / `tasks.py` / sidecars.

`scaffold-spec`-as-a-side-effecting-verb then disappears: its discipline lives once
in the builder (de-duplicating `:247`), its I/O is the universal persistence
effect. The side-effect was not load-bearing; it was hiding a duplication.

## 5. Decisions — when the human is asked, and how

### 5.0 When the human is asked at all — the consequence gate

**Decided (James, 2026-07-03): gate the human decision on *consequence*, not on
spec-cleanliness or every block boundary.** The dominant failure mode is **alarm
fatigue**: a verification layer that fires on everything gets rubber-stamped, then
disabled, and then protects *nothing*. A prompt's value is *inverse* to its firing
rate — so the design must ask **rarely and meaningfully**, or the whole propose
loop does not survive contact with a lazy human. (An earlier draft said "a clean
spec still needs a `y`" — that is exactly backwards.)

The gate is the Claude Code **auto-mode permission classifier**, applied to
transitions: escalate by *blast radius* — **irreversible committed spend** ×
destructiveness × shared-resource impact — not by whether the spec validated:

| Transition | Example | Human? |
|---|---|---|
| cheap + reversible | status snapshot, harvest (read-only), a self-cleaning canary | **auto-proceed** — no `y` |
| consequential | main-array submit (real core-hours), **even with zero conflicts** | **escalate** — `y` on the *spend* |
| genuine decision | a `conflict` / `choice` (§5.1) | escalate regardless |

This **preserves §1**: "the human makes every *decision*" — a cheap, reversible,
deterministic transition is not a decision, it is mechanical progress; the decision
was made at the consequential boundary. It is the *principled generalization* of
wave-4's park-before-greenlight-gated-blocks: that static set
(`submit-s2/s3/s4`, `aggregate-run`) was a **coarse proxy** for "consequential"; the
classifier is the real thing, scoring the transition (core-hours committed,
reversibility, destructiveness, shared-resource impact — the HPC analog of
read-vs-write / local-vs-outward).

**The hard floor.** The human may move the threshold *up* freely (more prompts).
They may move it *down* only to a floor that **never auto-proceeds** — irreversible
large spend, destructive/data-loss, anything outward-facing. Auto-proceeding the
cheap stuff exists precisely to *preserve* the human's scarce vigilance for the
floor cases, so the floor must be un-disableable even by a fatigued human trying to
make prompts stop. Attention is the budget; spend it on what is irreversible.

**Reversibility means irreversible *spend*, not cancellability.** For HPC the axis
is subtle: a job you can `qdel` has already burned core-hours — you can *cancel* it,
you cannot *undo the spend*. So a killable main-array submit is **not** cheap+reversible;
it commits irreversible cost the moment it dispatches. The gate scores by *committed
spend*, never by "can I stop it" — mis-scoring a killable job as reversible mis-gates
the exact transition the gate exists to catch.

**The gate must be instrumented, or it decays.** Calibration is not one-time.
Without measuring **override-rate** (how often the human reverses an auto-proceed)
and **rubber-stamp latency** (how fast they `y` — a proxy for not-reading), the gate
silently drifts into alarm-fatigue (over-fires → gets disabled) or unsafe
auto-proceed (under-fires → damage). Telemetry on those two signals is a build
requirement, not a tuning afterthought; an un-instrumented gate is one that decays
in whichever direction its miscalibration points.

**The choice invariant.** Once the gate (§5.0) says *ask*, **everything the human
is asked is a typed, code-enumerated `{choice}`.** Every point where the human
exercises judgment — a validator conflict, an ambiguous intent, a nudge that
targets a derived value — is surfaced that way.
The LLM never frames the options and never picks among them: **code enumerates, the
human selects, the LLM only renders and relays.** This is §1 ("no decision resolved
by the LLM") made mechanical at the review surface — if the LLM is choosing *which
levers to present* or *which to pull*, it is deciding. Everything in this section
is a consequence of that one rule.

### 5.1 Type the outcome, don't hand-classify the L2/L3 line

The L2/L3 line is **not** a maintained policy table. It is a property each
validator already implies — the **cardinality of valid fixes** — so give each
validator a typed outcome and route by shape:

| Validator result | Meaning | Route |
|---|---|---|
| `{ok}` | nothing wrong | pass |
| `{auto_fix, why}` | **exactly one** valid remediation, no value judgment | **apply + show** (never blocks) |
| `{conflict, why}` | **zero** valid remediations — infeasible | **escalate** (human redirects intent) |
| `{choice, options, why}` | **≥2** valid paths with a tradeoff | **escalate** (human picks — an L3 decision) |

The per-validator work is making each return the right outcome type (its author
already knows the cardinality). The line becomes a result-shape property,
lint-enforceable, not a hand-drawn table. Checked against the real validators:
`recommend-partition → auto_fix`; `validate-input-dataset → conflict`;
`validate-self-qos-limit → choice (cap/raise/split)`;
`validate-executor-signatures → conflict`.

**Decided (James, 2026-07-03): `auto_fix` applies-and-shows even when
research-consequential.** A unique deterministic fix (e.g. "walltime capped 8h→6h,
cluster p95") is *applied* but *surfaced in the render* and nudgeable — it never
blocks. "Silent-fix" means *does not branch*, never *hidden*. No information is
lost, latency is saved, and the human catches a fix they dislike at the same review
step they review everything else. Only `conflict` (infeasible) and `choice`
(tradeoff) block — and those are genuine L3 decisions the human was always going to
make.

### 5.2 All outcomes accumulate into ONE brief (never twenty questions)

The builder runs the **full** validator set every build, and the render presents
**all** non-`ok` outcomes at once — every `conflict`, every `choice`, every
`auto_fix` (shown). It **never** escalates the first conflict and re-asks. This
preserves the guiding doc's §3 "one brief, not twenty questions" — the exact
property `walk-submit-ambiguities` provided before it folded into the builder
(§4b). A build that stops at the first failing validator is a silent regression to
the interactive twenty-questions flow the fork abandoned; the invariant is
**collect-all-then-present**, and it is testable (a build with N independent
conflicts surfaces N, not 1).

### 5.3 `auto_fix` runs to a fixpoint, not a single pass

Validators form a dependency DAG (`qos` depends on `partition` depends on
`cluster`). An `auto_fix` that changes a field can invalidate a validator that
already ran. So the builder **applies auto-fixes and re-validates the affected
validators to a fixpoint** (or topo-orders the set) — not one pass. Otherwise it
applies a fix and ships a spec a downstream validator would have rejected:
validate-A-submit-B, the §4a hazard one layer up. The fixpoint terminates because
auto-fixes are monotone toward a valid spec; a set that does not converge is itself
a `conflict` to surface (with the cycle named), never an infinite loop.

### 5.4 A nudge on a derived value is a choice, not an LLM-picked input edit

The most common real nudge targets a **derived** quantity — *"make it cheaper,"*
*"under 500 core-hours,"* *"get it under 4 hours"* — not an input. Cost and runtime
are derived (`grid × walltime × cores`), and several input-edits satisfy any
target. If the LLM picked one lever it would be **deciding** (§1 forbids). So a
derived-target nudge is **inverted by code** into the candidate input-edits that
hit the target, and surfaced as a `{choice}`:

> under 500 core-hours via — (a) grid 100→50 · (b) walltime 8h→4h · (c) 4→2 GPU

The human picks; the builder re-runs with that input edit. The inversion machinery
(which inputs move a derived value, and by how much) is real code the builder owns
— the derived-field functions are already pure (§2), so they are sweepable or
invertible. A target with no satisfying edit is a `conflict`. This is why "the LLM
folds nudges into inputs only" (§8) is not a limitation: a nudge that *reads* as
being about an output is resolved by code into an input choice, never by the LLM
silently choosing a lever.

### 5.5 Ambiguous intent is a choice over code-enumerated candidates

When the human's NL maps to no unique intent value ("use the big cluster" with two
candidates), the LLM does **not** free-form a clarifying question — *which* options
it would present is a framing it can bias. Code enumerates the candidate values
(the two clusters) and surfaces a `{choice}`. Same shape as §5.4 and the validator
conflicts: **the LLM relays, code frames.** An ambiguity with no enumerable
candidate set (genuinely open-ended intent) is the one case that returns to
free-form dialogue — and it is a signal the field belongs in the *intent* bag as a
first-class human-judgment field, not something code should have resolved.

### 5.6 Trust boundary — untrusted evidence in the LLM's digestion

The propose loop is "code digests evidence → **the LLM drafts a proposal over that
evidence**." But some of that evidence is **untrusted**: error digests surface real
job **stderr / log text**, which comes from the cluster and the user's own executor.
A job can emit stderr containing injected instructions ("…propose deleting all prior
runs…") or merely misleading content that steers a debugging-fix proposal. The LLM
drafting a proposal *over attacker- or garbage-influenced text* is a real
prompt-injection surface — the one place translation touches untrusted input.

**The fork is structurally injection-resistant — claim it.** Because of §1 + §5.0,
the LLM **never executes**; it can only produce a *proposal*, and any consequential
or destructive proposal hits the human via the consequence gate's hard floor
(§5.0). So injection cannot auto-execute damage — at worst it produces a proposal a
human reviews. This is a genuine architectural advantage over an autonomous agent,
and it is worth stating: the human-in-the-loop + the hard floor *are* the mitigation.

Two things the design must nonetheless do:

- **Split trusted-structural from untrusted-raw evidence.** The trusted evidence is
  the *code-computed structure* — exit codes, task counts, the failed-wave ledger,
  extracted metrics (#355 reducers). Raw log/stderr text is **untrusted data**, and
  the LLM's digestion must treat it as *quoted data, never instruction*. The brief
  schema (§6) should carry the two separately so the renderer and the LLM both know
  which is which.
- **"Drafted over untrusted evidence" is itself a consequence factor (§5.0).** The
  residual surface is a proposal drafted over untrusted text that lands
  **cheap+clean and auto-proceeds unreviewed**. So the gate lifts the score of any
  proposal whose inputs include untrusted raw text — a debugging fix synthesized
  from stderr does not auto-proceed, even when the *action* looks cheap, precisely
  because its *provenance* is untrusted.

## 6. The renderer — one `render-brief` primitive over a brief schema

**Decided.** Rendering is a **single `render-brief` primitive**, not baked per
block. Two concerns hide in "the brief":

- **Digestion** — what evidence enters it (canary results, error digest, metrics
  table) — is *genuinely per-block*; blocks own it and emit structured brief
  **data**.
- **Presentation** — turning brief-data + provenance + validator-outcomes into
  faithful human text — is *uniform* and must not fragment.

Baking rendering per block gives N drifting copies (the exact failure just killed
in `block_chain`'s four `_next_block` copies). Decisively, the renderer must show
the **diff** (§4 of block-drive — what the nudge changed), which is inherently
*cross-brief*: a per-block renderer cannot diff against the prior proposal; a
central one keyed on `(brief_data, prior_brief_data, provenance, outcomes)` does.
The **brief schema** is the contract between digestion and presentation. This
mirrors the existing "reducers refuse to fabricate" split (#355): per-domain
computation, uniform presentation.

**The render is code-generated, never LLM free-prose.** An LLM narration can drift
from the spec, and the human would then greenlight a *description* that does not
match what runs — the fabricated-field bug class at the presentation layer. A
deterministic spec→prose renderer guarantees what-you-read-is-what-runs. The LLM
may add at most a one-line "why" gloss (its translator role); the field-by-field
breakdown is mechanical.

## 7. The raw spec — hidden by default, one gesture away, always journaled

Separate three things people conflate:

| Layer | Answer | Why |
|---|---|---|
| Default view | rendered breakdown only | raw spec is syntax; showing it reimports the burden the design removes |
| Availability | on-demand (expand / a command) | never *hidden* — that breaks trust, audit, debugging the builder |
| Record | always journaled | the decision journal's `resolved` **is** the raw approved spec — auditability is guaranteed at the record layer regardless of UI |

So "shown or hidden" is purely a UI-default question, and the default is *rendered
breakdown, raw spec one gesture away*. Availability is also what keeps the render
**honest in practice**: a deterministic renderer over a spec the human can
spot-check makes faithfulness *checkable*, not merely asserted — the same logic
that made the render code-generated. Availability + determinism are the proof.

### 7.1 What `y` freezes — the approval record `{intent, frozen_spec, input_hash}`

block-drive says `resolved` = *approved input spec*. But here the "input" is the
**intent bag** while the driver runs a **built spec** — and committing only one is
wrong both ways:

- `resolved` = intent alone → the driver must **rebuild** before running; if a
  default or the cluster state drifted since approval, the rebuild silently differs
  from what the human saw. Validate-A-approve-A-**run-B**.
- `resolved` = built spec alone → runs verbatim, but the intent for a future
  re-nudge is lost.

**Decided: `resolved` carries all three — `{intent, frozen_spec, input_hash}`.** The
driver runs the *frozen spec* (exact reproduction of what was reviewed); a re-nudge
restarts from the *intent*; the `input_hash` proves `frozen_spec == assemble(intent,
artifact)` at approval time (the content-address of §3/§4). This reconciles
block-drive's "resolved = input" (the intent *is* the input) with reproducibility
(freeze the built artifact too), and it is what makes "what is validated is what is
run" (§4a) hold across the approval boundary, not just within one build.

### 7.2 Submit-time re-validation — the review→execute TOCTOU

Freezing the spec (§7.1) guarantees you *run what you reviewed* — but not that what
you reviewed is *still valid*. The human reviews a spec resolved against cluster
state at T0, thinks for ten minutes, `y`s at T0+10, and submit fires at T0+11 —
by which point the qos may have tightened or the partition drained. Nothing
currently re-checks at that boundary.

**Decided: a cheap submit-time re-validation of the frozen spec against current
state** — a re-*check* (run the L2 validators against the live cluster), **not** a
re-*resolve* (do not silently rebuild — that would change what the human approved).
On failure, re-surface it: "the cluster changed since you approved" → back into the
propose loop, with the changed constraint named. This is the guaranteed-harvest
discipline (§5 of the guiding doc) applied to the pre-submit edge: no path from
`y` to execution ends in a stale-spec surprise.

## 8. The loop, restated with the separation

```
code digests evidence
  → LLM drafts the intent bag           (flat values, typed tool — §2a)
  → build(intent) → (spec, provenance)  (resolution effectful §4 → assembly pure §3)
  → validators → typed outcomes         (ok | auto_fix | conflict | choice — §5)
  → render-brief                        (faithful, provenance-tagged, diff-aware — §6)
  → consequence gate (§5.0)             cheap + reversible + clean → AUTO-PROCEED (no human)
  → else present; human sees ONLY logic (L3)  (ALL outcomes at once — §5.2)
  → y  |  NL nudge
      → nudge on a derived value?        → code inverts → {choice} (§5.4); human picks
      → else LLM folds nudge into INTENT (never a derived output — the fabricated-field guard)
      → re-build → re-validate to a fixpoint (§5.3) → re-render as a DIFF
      → loop until y
  → on y (or auto-proceed):             commit {intent, frozen_spec, input_hash} (§7.1)
  → at execute:                         submit-time re-validate the frozen spec (§7.2)
```

The human's cognitive load is bounded to **L3 on consequential transitions** — the
consequence gate (§5.0) spends their scarce vigilance on what is irreversible and
auto-proceeds the cheap+clean. L1 is impossible by construction; L2 is
applied-and-shown (`auto_fix`) or surfaced as a decision (`conflict`/`choice`);
everything syntactic is either normalized-and-shown or one gesture away.

### Latency

- **No LLM↔validator loop anywhere** — L1 is impossible (§2), so there is nothing
  to bounce back to the LLM for shape.
- Cold first draft = one generation (nothing to overlap it with, but it is a single
  call).
- **Speculative validate/render** on the *re-present* path: the human reads the
  diff of nudge N while the build+validate+render for it already ran during that
  read (the same speculation pattern as the canary). It cannot hide the cold draft
  — there is nothing to overlap yet — but that is a single call, not a loop.

## 9. What this reuses (not net-new machinery)

- **L1** = the spec-contract `model_validate` the integration tier already does
  (promoted test-time → runtime-pre-presentation, and mostly a no-op because
  construction *is* validation).
- **L2** = the existing `validate-*` primitives, re-fronted to return typed
  outcomes.
- **The builder** = `build-submit-spec` + the resolution layer (`resolve-*`), with
  `apply-safe-defaults`/`walk-submit-ambiguities` folded in and `scaffold-spec`
  dissolved (§4b–c).
- **Provenance** = the same doctrine as `provenance-manifest`, one level finer.
- **The diff** = the wave-4 §4 field-ownership changed-fields.
- **The faithful-render invariant** = #355 "reducers refuse to fabricate," applied
  to the brief.
- **Content-addressed resolution** = the `canary_cache` `(cmd_sha, version, TTL)`
  pattern, generalized; the campaign sidecar-freeze is its hard special case.
- Enforcement lives **in the verb, not prose** — the builder validates, the
  renderer is code, the validators gate; no honor-system "LLM, make your spec
  valid."

## 10. Build checklist (gated on the proving run)

1. **First-class builder — two layers** (§3): effectful *resolution* (SSH/reads →
   artifact) + pure *assembly* (`intent + artifact → spec, provenance`). Fold in
   `apply-safe-defaults` + `walk-submit-ambiguities`; keep `resolve-*` as the
   composed resolution layer; dissolve `scaffold-spec` into pure-emit + the generic
   `materialize` effect (§4b–c).
2. **Content-addressed resolution artifact** (§4) — key + TTL; generalize the
   campaign sidecar-freeze; **close the preflight→build coherence gap (§4a)** so
   what is validated is what is built.
3. **Typed validator outcomes + the choice invariant** (§5) —
   `{ok|auto_fix|conflict|choice}`; a lint that every `validate-*` returns the
   shape; a router that applies `auto_fix`, **accumulates all outcomes into one
   brief (§5.2)**, and **re-validates auto-fixes to a fixpoint (§5.3)**; the
   **derived-value inversion (§5.4)** that turns "make it cheaper" into a
   code-enumerated `{choice}`; ambiguous-intent enumeration (§5.5).
4. **Per-block intent schema** — the human-judgment vs auto-resolvable partition
   exists only for submit (`field_partition`); build it for
   status/aggregate/campaign (the same gap wave-4 flagged for field-ownership).
5. **`render-brief` primitive** (§6) over a brief schema — faithful (code-gen),
   diff-aware, consumes provenance + outcomes; blocks emit brief *data*.
6. **Journal the provenance**, not just `resolved` — record which fields were
   `auto_fix`ed vs human-chosen, so "the human made every decision" is *auditable*
   (§7 records the spec; this records *how* each field got there).
7. **Raw-spec disclosure** (§7) — default hidden, on-demand available, `resolved`
   already journals it.
8. **Speculative validate/render** on the re-present path (§8).
9. **Consequence gate + telemetry** (§5.0) — a transition classifier scoring
   **irreversible committed spend** × destructiveness × shared-resource impact
   (not cancellability), auto-proceeding cheap+clean and escalating
   consequential/decisions, with the un-disableable hard floor; generalizes the
   wave-4 gated-block set. **Instrument override-rate + rubber-stamp latency** so it
   does not decay. This keeps the loop from being turned off.
10. **Approval record + submit-time re-validation** (§7.1–7.2) — commit
    `{intent, frozen_spec, input_hash}` on `y`; re-*check* (not re-resolve) the
    frozen spec against live cluster state at execute, re-surfacing on drift.
11. **Trust split** (§5.6) — carry trusted-structural vs untrusted-raw evidence
    separately in the brief schema; the LLM digests raw text as *data, not
    instruction*; a proposal drafted over untrusted text lifts its consequence
    score (§5.0) so it never auto-proceeds unreviewed.

Nothing here requires a raw-API structured-output dependency (§2a) — the intent
bag rides the typed-tool surface the fork already has, which keeps #137 dissolved.

## 11. Open seams (flagged, not yet resolved)

Distinct from §10 (things to *build*) — these are judgments still open:

- **Render altitude.** Faithful ≠ decision-relevant. A code render can be perfectly
  faithful and still an unreadable field dump (`mpi_pe: dc*, qos: normal`). The
  render must present at the right altitude — what changed, what matters — which is
  a *curation* judgment with risk on both sides: dump everything (unreadable) or
  curate (and hide something the human needed). The diff (§6) and provenance (§3)
  help, but "which fields are decision-relevant" is unspecified. Candidate rule:
  surface `human`-origin + `changed` + non-`ok` outcomes by default; fold
  `default`/`invariant`/`derived` into an on-demand tier — but this needs a real
  brief in front of a real researcher to calibrate.
- **Cross-block intent.** The intent→build design here is single-block. But a nudge
  at block N can target block M's fields ("hold walltime, halve the grid" during S2
  edits S3's inputs — wave-4's `advance_carrying`). The seam between "intent bag for
  block N" and "this nudge edits block M" must connect to wave-4's field-ownership
  routing ([`block-drive.md`](block-drive.md) §4), not re-derive it.
- **Intent-schema authority.** §10.4 says build the per-block intent schema — but
  *who owns* the human-judgment-vs-auto-resolvable line per family is itself a
  design call (submit's `field_partition` encodes an incident-hardened lock;
  the other three families have no equivalent doctrine yet). The schema is not
  merely absent, it is *un-adjudicated*.
- **Expensive-validator cost, and the double-run.** Some L2 checks are SSH/data-heavy
  (`validate-input-dataset` reads the data; qos reads live limits). The §5.2 full-set
  run × §5.3 fixpoint × per-nudge re-build can re-pay those unless **validator
  outcomes are memoized** (content-addressed on the fields they read — the §4 idea
  extended from resolution to validation). Separately: `recommend-partition` runs
  *inside* resolution (to pick a partition) **and** as an L2 gate (to validate the
  pick) — the resolution-uses-validators vs L2-gate-validators relationship is
  unspecified and risks a double SSH.
- **Builder failure is neither a conflict nor a spec.** If assembly/resolution
  *crashes* (bug, missing cluster config, corrupt prior) there is no valid spec to
  render — the one path the faithful renderer cannot render. Degrade to the raw
  error + the raw-spec path (§7)? Undefined.
- **First-present diff baseline.** §6 renders a diff "vs the prior proposal," but the
  cold first proposal has no prior. Diff against defaults? the prior run
  (re-submit/campaign iteration)? nothing? The renderer contract is undefined at t=0.
- **The human can respond with a *question*, not `y`/nudge.** "Why did it pick
  partition X?" is neither approval nor an edit. The `y | nudge` binary does not
  model the LLM answering (translator role) *without* advancing the driver — a real
  review turn the loop currently omits.
- **Multiple simultaneous pending decisions.** A researcher with several runs /
  campaigns has N pending briefs; the design is single-proposal. No queue, no
  **consequence-ranked prioritization** — though the gate (§5.0) already computes the
  score, so it can order the queue (highest blast-radius decision first).
- **Greenfield / cold-start boundary.** intent→build assumes the experiment *exists*
  (`tasks.py`, an entry point). *Defining* the experiment (`wrap-entry-point` /
  scaffold) is a **pre-spec** surface — the intent bag has no meaning before it. A
  scope carve-out like §1's fix/interpretation one; do not stretch intent→build over
  experiment-definition.
- **Scope-expanding nudges.** "Also sweep the learning rate" adds an **axis**,
  touching the task structure / executor — not a value-edit to an existing field.
  That is closer to a re-scaffold than a spec edit; the boundary between a
  *value-nudge* and a *structure-nudge* is unspecified.
- **Mistranslation × auto-proceed (a feature, stated so it is not misread).** If the
  LLM misreads "small test" as a full run, the gate (§5.0) catches it (consequential
  → escalate), so the gate *bounds* mistranslation blast radius. But a mistranslation
  that stays *within* cheap auto-proceeds unreviewed. That is the gate working
  (bounded damage), not a hole — but auto-proceed must not be read as
  "mistranslations cannot ship."
