---
status: shipped
---
# The notebook-audit substrate — design + implementation plan

**Status: v1 + v1.5 + v1.6 IMPLEMENTED (2026-07-08).** v1.6 = the FULL-VIEW
RECOMPUTE upgrade: the audit's CONFIGURATION (`input_roots` / `source_roots` /
`attention_order`) is now persisted on the `audited_source` block, so the T8
sign-off gate RECOMPUTES `view_sha` (one definition,
`ops/notebook/canonical.py::build_canonical_view`) instead of validating it
present — the "statically-recomputable legs only" boundary is RETIRED (see the
v1.6 drift note). Core (T0–T9) + the skill +
the verbs (`notebook-lint`, `notebook-audit-view`, `notebook-status`,
`notebook-auto-clear`, `notebook-record-receipt`) are in the tree, plus
the v1.5 layer: journaled sha-bound render receipts (T10), verify-relay
notebook claims (T11), `attention_order` (T12), the sidecar/dossier audit
echo (T14), the normative harness contract
(`docs/internals/harness-contract.md`), and the jupytext EXPORT plugin
`examples/plugins/hpc-agent-notebook-render` (`notebook-render` +
`notebook-ingest-signoffs` — the portability artifact and the
second-conforming-harness proof). Cite `path::symbol`, never line
numbers. Implementation drift is recorded in the drift log at the end of
this document.

## Product intent

Let a user arrive with an **idea** and leave with **audited experiment code**
the submit pipeline will accept. Today the pipeline assumes fleshed-out code
(wrap-entry-point decorates an existing function). The prelude inverts it:
idea (human words) → LLM drafts source → human audits → GRADUATION extracts
the audited entry point → the submit pipeline refuses entry points not
hash-linked to a current audit (opt-in; undisciplined repos byte-identical).

Competitive frame: Claude Science ships four-component provenance (exact
code, environment, description, message history) with an LLM reviewer bolted
on after; this gates compute on deterministic checks + human sign-off BEFORE,
and treats the auditor's attention as the scarce resource.

## Architecture decisions (settled)

- **D-source (user, 2026-07-07): the LLM drafts RAW PYTHON, never .ipynb.**
  Source of truth = a plain `.py` in jupytext percent format (`# %%` cells)
  carrying opaque section markers as plain comment lines
  (`# hpc-audit-section: <slug>` as the first non-blank line inside a cell —
  deliberately NOT jupytext metadata syntax, so core never learns jupytext's
  grammar). The NOTEBOOK is a deterministic caller-side RENDER (jupytext +
  execution) used only to display audit views and collect responses;
  adjustments are deltas to the `.py`, re-rendered — the notebook is never
  hand-edited. Precedent: the target repo's own doctrine (harxhar README:
  `src/` is the source of truth; notebooks are views). jupytext/nbclient are
  the renderer's deps — **plugin-side, never core; net new core deps: zero.**
- **D-attention (user, 2026-07-07, supersedes the uniform-cheap sign-off):
  TIERED sign-off — the auto-mode-classifier pattern.** Code computes each
  section's tier from the audit view: (a) **auto-cleared** — empty
  diff-from-template, zero lint flags, declared assertions green → journaled
  as `auto_cleared` with hashes, mechanical, never claiming human review, no
  human attention spent; (b) **human-required** — nonempty template diff,
  flags, or failed/absent assertions → an EFFORTFUL sign-off: the human's
  utterance must engage the section's specifics (token-derivation can require
  naming something from the diff/flags, not just the slug). Rationale:
  concentrating attention where judgment happened prevents rubber-stamp
  fatigue; rarity buys seriousness. The graduation gate requires every
  template section to be EITHER auto-cleared at its current hash OR
  human-signed at its current hash.
- **D3 — audit identity = a fourth decision-journal scope kind `"notebook"`**
  (`state/decision_journal.py::SCOPE_KINDS` + a path branch →
  `.hpc/notebooks/<audit_id>.decisions.jsonl`). Sign-offs are ordinary
  append-decision records (`block="notebook-sign-off"`,
  `resolved={audit_id, section, section_sha, view_sha}`); append-only, the
  existing flock + gate stack for free. Caller-authored slug ids — never
  core-invented (the fabrication class).
- **D5 — sign-off un-fakeability, three locks:** (1) no sign-off verb, no
  chain/next_block/skill affordance — append-decision or nothing (the
  no-unlock-verb doctrine, `docs/design/rigor-primitives.md`); (2) the gate
  RECOMPUTES `section_sha` from the `.py` on disk at append time — a hash
  cannot be asserted into existence; (3) the authorship bar — bare acks
  refused (`ops/decision/journal.py::_is_bare_ack`), harness-utterance tier
  with token-exact naming (the #26 precedent), tightened per D-attention for
  human-required sections. `view_sha` binds what-the-human-saw into the
  record (archive vs interface separation — the audit trail records the
  projection shown, not just the content covered).
- **D6 — archive vs interface.** The complete record (source + journal) is
  the archive; the INTERFACE is `notebook-audit-view`: a deterministic
  canonical-JSON per-section projection — diff-from-template over `.py`
  source segments (stdlib difflib; classified inherited/added/modified by
  source-hash), `ast.Assert` assertion table, lint flags, cell hash links —
  with `view_sha = sha256(canonical_json)` and a code-rendered markdown
  projection (the `ops/relay_render.py` posture). NO LLM-freeform prose in
  the audit path; prose relayed about a section goes through the rule-10
  verify-relay machinery (v1.5 generalization).
- **D7 — opt-in:** `audited_source: {source: <py relpath>, audit_id,
  template: <py relpath>, rendered_notebook?: <metadata, never hashed>}` on
  `_wire/actions/interview.py::InterviewSpec`, persisted in interview.json.
  Absent → every gate passes silently, byte-identical (the
  `ops/scope_gate.py` fail-safe posture).
- **D8 — graduation gate, one definition, two synchronous seats:**
  `ops/notebook_gate.py::assert_source_audited` — recompute `module_sha` +
  per-section hashes + `linked_sources` hashes; refuse
  `errors.SourceUnaudited` naming drifted/unsigned sections. Seats:
  `ops/resolve_submit_inputs.py` pre-sidecar (the S1 human boundary) and
  `ops/submit_flow.py` pre-staging. Drift = unsigned by construction (a
  signed section edited afterward simply reads unsigned at its new hash; no
  drift state machine). Fires+passes pair + enforcement-map rows required.
- **D9 (revised) — outputs/freshness live with the RENDERER:** the caller-side
  render executes in the experiment env and emits a render receipt
  `{section_slug: {output_sha, error: bool}}`; core (v1.5) merges/compares
  opaque hashes only. Core parses no ipynb at all in v1. nbformat / nbdime /
  jupyter deps: rejected for core.

## v1 task list (file-disjoint for parallel Opus dispatch)

Wave A (parallel): **T1** `state/audit_source.py` (new) — percent-format
section model: marker parse, segmentation, `section_sha`/`module_sha` over
normalized source segments; templates parsed by the same function; slug
validation via the shared run-id pattern. Tests: crafted percent-format
strings. **T2** interview opt-in — `_wire/actions/interview.py` +
`ops/memory/interview.py` persist `audited_source` verbatim; absent →
byte-identical interview.json. **T3** this design doc → final (status flip +
any drift found during implementation).

Wave B (after T1, parallel): **T4** `ops/notebook/lint.py` — `verb=validate`
primitive: structural completeness (template marker slugs as an
order-preserving subsequence), executes-live (path-shaped string literals vs
caller-declared opaque `input_roots`; computed paths = a recorded
`unverifiable_paths` gap), and the `linked_sources` report (imports resolving
under caller `source_roots` → file → `module_sha`; recorded at sign-off,
drift-checked by T9 — strictly stronger than a display-cell check, which the
render makes unnecessary by construction). Findings are reported, never
raised — the gate refuses, the lint reports. Each rule needs its
fire-on-synthetic-violation test. **T5** `ops/notebook/audit_view.py` — the
deterministic view + `view_sha` + the TIER computation (D-attention) +
markdown projection. **T6** `state/notebook_audit.py` + `notebook-status`
query — newest-first reduction to
`auto_cleared | signed_current | signed_stale | unsigned` per section.

Wave C (sequential, one at a time — these files are hot): **T7**
`state/decision_journal.py` — the `"notebook"` scope kind. **T8**
`ops/decision/journal.py` — `_assert_signoff_authorship` (D5 + D-attention
tiering: an auto-cleared section REFUSES a human sign-off record as
unnecessary-affordance? No — accepts but marks redundant; decide in
implementation with a recorded reason), wired beside
`_assert_unlock_authorship`; contract test pinning the no-affordance rule.
**T9** `ops/notebook_gate.py` + `errors.SourceUnaudited` + the two seats +
enforcement rows.

v1.5 (designed-for, deferred): **T10** freshness via render receipts; **T11**
verify-relay section-hash claims; **T12** caller-supplied attention-ordering
config; **T13** the thin skill (drives draft→lint→view→relay-verbatim→
sign-off→status; free-text elicitation; no Edit of source during audit);
**T14** sidecar `audited_source` echo for the dossier.

## The audit SURFACE — harness-first (user decision 2026-07-08, supersedes
## the interactive-notebook renderer)

**The Claude Code harness IS the v1 audit surface.** Rationale: the harness
does the notebook's load-bearing jobs strictly better — sign-offs are typed
chat utterances (the STRONGEST authorship tier; the notebook flow was always
the degraded tier-2 path), and iteration is the existing y/nudge rendezvous
loop pointed at code: view relayed → human signs or nudges → the LLM
re-drafts the section (drafting is its sanctioned prelude role) → the hash
moves → the section reads UNSIGNED again by construction → re-lint,
re-view, re-sign. No stale approval survives an edit; every step gated,
journaled, relay-audited. A stateful notebook kernel is what the determinism
doctrine distrusts anyway.

v1 surface work: the thin SKILL (T13, PROMOTED to v1) — drives draft → lint
→ audit-view relayed VERBATIM → typed sign-off via append-decision → status,
in-session; optionally a harness Artifact page for figure/diff-rich views.
Plus the one notebook capability the harness genuinely needs supplied: an
**execution contract** — the caller env runs the sections and emits the
render receipt ({slug: {output_sha, error}}) + a captured-outputs directory
the harness can display as images.

**THE HARNESS CONTRACT (user decision 2026-07-08: harness-agnostic, but a
harness is REQUIRED).** The audit loop is defined against three capabilities
any conforming harness must provide, not against Claude Code: (1) an
out-of-band HUMAN-UTTERANCE LOG the LLM cannot write (the full-strength
authorship tier's channel — document the write API so alternative harnesses
can implement it); (2) a relay/verbatim enforcement point (the Stop-hook
role); (3) backgrounding/wake for detached waits. Claude Code satisfies all
three via hooks. The tier machinery already degrades honestly when a
capability is absent (the journal-response tier). The CLI stays the
invariant substrate (the block-drive doctrine); MCP and skills are
projections. THIS CONTRACT is the vendor-lock-in defense — implementations
compete under it. The normative spec (the three capabilities + the utterance-log
write API a second harness implements against) now lives at
`docs/internals/harness-contract.md`.

The jupytext notebook EXPORT is **SCHEDULED v1.5 (user decision 2026-07-08:
build it — vendor-portability rationale replaces the earlier
wait-for-a-trigger deferral)**: a projection over sealed records (source +
template + receipt), plugin/tools lane (jupytext never enters core). Two
roles, in order: (a) the PORTABILITY ARTIFACT — audits readable anywhere,
no harness; (b) the ceiling: a SECOND CONFORMING HARNESS — a human typing
into a notebook sign-off cell IS out-of-band from the LLM, so a render that
writes that text through the documented utterance-log API provides the
full-strength tier with no Claude Code anywhere. The execution-receipt
emitter remains ~30 lines of caller-side convention. **v1 = core + the
skill; v1.5 = the export.**

Reuse accounting (why v1 is thin): greenlights, unlocks, scope locks,
sign-offs — and the future registration kernel — are instances of ONE
primitive, and **(user decision 2026-07-08) that primitive becomes a
FIRST-CLASS foundational object: the ATTESTATION** —
`{attestor: human|code, subject_kind (opaque), subject_id, content_sha,
view_sha?, evidence}` riding EXISTING decision-journal records (no new
store, no migration). Human attestations face the authorship gates; CODE
attestations (auto-clear records, reproduction receipts, look records) face
recompute — the machine-side records are the same object.

**NEW TASK T0 (precedes T6/T8): the attestation kernel** —
`state/attestation.py`, ~100 lines, three functions every instance routes
through: `bind` (recompute-and-compare at append — the un-fakeable lock,
extracted once), `reduce` (newest-first → `current | stale | absent` —
drift-revocation defined once), and the record-shape validator. **Gates stay
thin and per-instance and CALL the kernel** — explicitly NOT a parametric
mega-gate (the instances differ in load-bearing ways: greenlight routes
next_block, unlock is directionally asymmetric, sign-off carries tiers; a
flag-soup unified gate would be harder to audit than four small gates).
T6/T8 instantiate the kernel rather than becoming the fourth divergent
copy; greenlight/unlock migrate opportunistically (their records already
fit; the reducer generalizes theirs). Enforcement row required: any new
attestation-shaped feature routes through the kernel (the one-definition
rule applied to the primitive itself). This supersedes the earlier
member-four refactor trigger — the cheapest moment to introduce the object
is immediately before its next instantiation.

Genuinely new core beyond the kernel: the section model, the lints, the
view/tier logic. Product formulation: the journal is a chain of
attestations; the dossier is sealed attestations; a receipt is a code
attestation — every trusted thing in the system is one of these and
nothing else.

The product claim this ordering earns: the harness + this substrate is a
REPL where every cell has provenance, every approval has authorship, and
every edit revokes stale trust — "we also export notebooks," not "we are
one."

## Boundary-drift flags (Q1 watch list)

executes-live must never grow a reader-function vocabulary (read_csv etc. —
that needs a Q2 assembly point / pack matcher); template slugs stay opaque
(content-meaning checks are pack territory); linked-sources judges import
ORIGIN IDENTITY only; marker syntax stays comment-only (jupytext metadata
would couple core to its grammar); the render receipt stays opaque
`{slug: sha}` — parsing an output crosses Q1; sign-off UX pressure to soften
the human-required tier is the feature working — soften only via richer
harness-captured utterances, never bare acks.

## Implementation drift log (v1, 2026-07-08)

Deviations from the plan above, each with its recorded reason:

- **`notebook-auto-clear` is a NEW mutate verb the plan lacked.** D-attention
  says auto-cleared sections are "journaled as auto_cleared" but no planned
  task owned the agent-facing writer — without it, template-inherited
  sections could never pass the graduation gate. The verb
  (`ops/notebook/auto_clear_op.py`) recomputes lint + view + tier entirely
  server-side (caller supplies only paths/ids/roots), so a caller cannot
  launder a flagged section by omitting findings; records route through
  `state/notebook_audit.py::record_auto_clear` → `attestation.bind`.
- **T5's view is a pure module; the interface verb is `notebook-audit-view`**
  (`ops/notebook/view_op.py`), which takes `lint_findings` CHAINED from
  `notebook-lint` (the lint's rules live inside its primitive body; no clean
  shared function existed). The view result carries the code-rendered
  `markdown` for verbatim relay.
- **T8 accept-and-mark decision (D-attention open question resolved):** an
  AUTO_CLEARED section ACCEPTS a human sign-off and stamps
  `resolved["redundant"] = true` — refusing would delete a real human review
  and create a verb-shaped affordance gap; marking keeps the attention
  ledger honest. The raised diff-token bar is waived for redundant
  sign-offs; the recompute lock and slug-naming floor still apply.
- **T8 tier-recompute boundary — RETIRED by v1.6 (see the v1.6 drift note).**
  ~~At append time the sign-off gate checks the statically-recomputable tier
  legs (diff classification, assertions-without-receipt) with
  `lint_findings=()`; a section made human-required solely by a lint flag is
  not distinguished at the gate. `view_sha` is validated present but never
  recomputed there — it is a provenance witness; the recompute lock is
  `section_sha`. No resolvable template → every section reads added →
  HUMAN_REQUIRED (conservative).~~ The gate now RECOMPUTES `view_sha` in full
  (real lint from the recorded roots, journaled receipts, recorded order), the
  tier is real, and an absent template is REFUSED (not softened).
- **T6 stale auto-clear reduces to `unsigned`, not a stale-flavored status:**
  drift = unsigned by construction; a machine clearance has no human to
  inform. A stale HUMAN sign-off earns the informational `signed_stale`.
  Both fail the gate identically.
- **T9 refusal split:** missing/unparseable source or template in an
  opted-in repo raises `SpecInvalid` (broken setup), not `SourceUnaudited`
  (reserved for present-but-not-current sections). `SourceUnaudited` reuses
  `error_code="precondition_failed"` (the ScopeLocked precedent — no new
  wire enum).
- **T0 kernel additions:** `reduce` takes records in append order (newest
  LAST — the order `read_decisions` returns) and grew an optional
  `subject_id` filter so per-section callers don't re-write the selection
  loop. The kernel operates on the attestation *projection* built from a
  journal record's `resolved` fields — it never learns the journal record
  shape.
- **T1 marker edge:** a col-0 marker that is not its cell's first non-blank
  line is REFUSED loudly (`SpecInvalid`); an indented marker is ordinary
  content (not a marker at all). Preamble before the first marker belongs
  to no section but IS covered by `module_sha`.
- **`ops/notebook_view.py` facade** exists solely so
  `ops/decision/journal.py` can reach the view builder without tripping the
  subject-imports lint (the `field_ownership.py` precedent).
- **Skill registration:** `hpc-notebook-audit` is auto-discovered by the
  installer; deliberately NOT in `_KNOWN_SKILLS` (that set gates the
  sub-skill return-envelope protocol, which this in-session human-facing
  driver doesn't use — the hpc-submit posture).

### v1.5 drift (2026-07-08, same day)

- **The receipt-laundering hole is CLOSED (supersedes the v1 receipt
  behavior above):** `NotebookAutoClearSpec.receipt` is DELETED — the
  mutate path reads only JOURNALED receipts
  (`state/notebook_audit.py::read_render_receipts`), each a code
  attestation bound to the section sha at record time
  (`record_render_receipt` → `attestation.bind`), stale-by-construction
  on drift. The new `notebook-record-receipt` verb (registry 138) parses
  the source on disk so a receipt can only ever be recorded against
  current source. The read-only view keeps an inline `receipt` for
  preview (journals nothing; sha-bearing entries are freshness-gated,
  sha-less inline entries keep v1 preview behavior).
- **Truthfulness caveat (adversarial review F8, 2026-07-07):** T10 closed
  **freshness**, not **truthfulness**. `output_sha`/`error` are
  CALLER-ATTESTED per D9 — `notebook-record-receipt` recomputes the sha
  bind, not the outcome, so an emitter *could* journal `error=False`
  without executing. The honest claim is that a receipt vouches only for
  the exact on-disk bytes and drifts stale on any edit; the trust boundary
  is the emitter (same class as a conforming harness's out-of-band writes),
  and the graduation consumers WEIGH the caller-attested outcome rather
  than re-deriving it. Docs corrected to stop implying the verb "never
  trusts a caller-supplied receipt" (it never trusts an *inline* one).
- **T11 reuses contradiction kinds** — a wrong section-status/passed
  claim is kind `state`, a sha mismatch is kind `number`; no wire enum
  change, no new blocking-set entry. Notebook relay verification lives in
  `ops/decision/verify_relay.py::verify_notebook_relay`, a sibling of the
  run primitive (the run CLI surface is byte-identical).
- **T12 `attention_order`** defaults to source order; listed slugs first,
  unknown ignored, unlisted keep source order; the presented order feeds
  the module `view_sha` (it changes what the human saw).
- **T14 vocabulary:** dossier store nouns `audited-source` (source AND
  template .py — same store kind, distinguished by archive path, not a
  role field) + `notebook-journal`; `audit_id` joins the identity
  projection (emitted only when audited, the `reproduces` precedent).
  The sidecar echo `{source, template, audit_id}` drops
  `rendered_notebook` (metadata, never sealed) and is stamped after the
  graduation gate passes at resolve time.
- **The plugin** (`hpc-agent-notebook-render`) is the first plugin to
  register `@primitive` verbs; it ships NO JSON schemas (the Pydantic
  spec_model validates at the CLI seam — a hand-written schema would only
  add drift surface), re-derives the harness-injection filter rather than
  importing the private hook regex, and keeps `--no-deps` in CI with an
  explicit render-stack install step. `notebook-ingest-signoffs` writes
  typed sign-off-cell text through the documented utterance-log API
  (no-scaffold honored — absent namespace reported as the degraded tier)
  and lands sign-offs through the ordinary append-decision gate.

### v1.6 drift (2026-07-07) — FULL-VIEW RECOMPUTE, the retired boundary

- **The "statically-recomputable legs only" boundary is RETIRED.** The T8
  sign-off gate no longer validates `view_sha` as merely PRESENT — it
  RECOMPUTES it in full and refuses a mismatch. Root cause of the old
  boundary: the audit's CONFIGURATION (`input_roots` / `source_roots` /
  `attention_order`) was per-invocation ephemera, never persisted, so the gate
  lacked the lint findings. It is now persisted verbatim on
  `_AuditedSource` (interview.json's `audited_source` block), all three fields
  OPTIONAL and defaulting to `None` so an `exclude_none` write keeps
  interview.json byte-identical to a pre-upgrade record (absent → conservative
  defaults: empty roots, source order). The D7 absent-block byte-identity pin
  is untouched.
- **One definition: `ops/notebook/canonical.py::build_canonical_view`.** It
  parses source+template, RECOMPUTES the lint in-process from the recorded
  roots (the auto-clear un-fakeability precedent — never caller findings),
  reads JOURNALED fresh receipts, and builds the D-attention view with the
  recorded order. The gate reaches it through the `ops/notebook_view.py`
  facade (subject-imports lint); the `notebook-audit-view` /
  `notebook-auto-clear` verbs and the render plugin all route through the SAME
  helper, so their per-section `view_sha`s agree with the gate's by
  construction.
- **Refusal taxonomy (the loud, specific message).** The bind lock covers
  section-body drift and the trusted-display lock covers a stale render, so a
  `view_sha`-ONLY mismatch (bind + render both current) means a VIEW ingredient
  moved: a lint finding changed (a data path under `input_roots` vanished or
  appeared), a journaled receipt changed, or the attention order changed. The
  refusal names that class and tells the human to re-run `notebook-audit-view`
  and re-inspect. The tier is now REAL: a section human-required SOLELY by a
  lint flag refuses a bare-slug sign-off (the closed gap).
- **TEMPLATE is now REQUIRED at sign-off** (was: absent → conservative
  HUMAN_REQUIRED). The canonical view is a diff-from-template projection and
  every sanctioned `view_sha` was produced against a real template, so an
  unresolvable template means the signed view is not reproducible — refused
  loudly.
- **`notebook-audit-view` grows `canonical: bool`** + optional
  `input_roots`/`source_roots`. The DEFAULT flow (no override) recomputes the
  canonical view and reports `canonical: true`; explicit roots/order differing
  from the recorded config, explicit `lint_findings`, or an inline `receipt`
  yield a PREVIEW (`canonical: false`) whose view_shas the gate may refuse.
- **The plugin `notebook-render` / `notebook-ingest-signoffs` build the
  canonical view** (was `build_audit_view(..., lint_findings=())`) so their
  recomputed view_shas are not refused by the upgraded gate whenever a lint
  flag fires.

## Related, planned separately

The palatability projections the same review surfaced: the **run story** (a
code-rendered timeline of a run's journal trail — the decision journal's
interface sibling) and the **attention queue** (status-snapshot v2: fleet
overnight digest ordered by needs-your-verdict-first). Both pure
ordering/identity projections; natural siblings of T5's renderer posture.

## Amendment (2026-07-07, user-ruled during run #10): hyper-palatable sign-off

1. **The next-actions footer (render-only, no canon bump — view_sha rolls
   from section shas, the markdown is generated from the view):** the view's
   markdown ends with the literal copy-ready utterances — per human_required
   section the sign line, the batch form, the nudge form, each citing the
   section's view_sha12. Old renders go stale on landing; remedy = re-run
   view (existing). Until built, the SKILL requires the relaying agent to
   end every view relay with the same line (mechanical quote of structured
   fields, not interpretation).
2. **The y-adoption tier for T8 (design-flagged, not yet built):** extend
   the greenlight pattern — CODE drafts the sign-off proposal from the
   current view (sections + view_shas; never LLM-drafted), human types `y`
   to adopt. GUARDRAIL: T8's tiered bar survives — redundant sign-offs and
   high-attention tiers KEEP their diff-token/typing cost; y reaches only
   the bottom of the ladder. Rarity-buys-seriousness is load-bearing;
   palatability must never reach the effortful tier.

## Amendment 2 (2026-07-07, run-#10 live findings): the MCP projection

Run #10's prelude priced the audit loop's MCP absence: hand-authored spec
JSONs, two schema fumbles (caught loudly — the guard held; cost was
latency). Ruled projection, three parts: (1) the five audit verbs join
`_CURATED_EXTRA_VERBS` (typed tools kill hand-authored JSON; one frozenset
edit + tests/test_mcp_curated.py) — post-run, never mid-audit (wheel move).
(2) The SIGN-OFF rides MCP ELICITATION when Phase 1 lands — the audit loop
is the elicitation binding's flagship seat: server→client→human-typed→
server, the utterance never passes through the model (stronger authorship
than agent-forwarding). (3) A block-drive-style loop driver is REJECTED:
the audit loop's sequencing alternates with human acts at every step — the
human is the sequencer, and run #10's live evidence is the skill-driven
loop holding without improvisation. ALSO from the same run: standalone
audits (no interview `audited_source`) run ROOTLESS-canonical — no seat
records the audit configuration, so the template-mandated `source_roots`
binding is silently inactive (view_op.py reads interview.json only). Fix:
a config-recording seat for standalone audits + audit-preflight flagging
rootless audits; executes-live gains an `output_roots` allowance (output
literals currently flag as noise).
## Amendment 3 — relay-due discharge (the omission gate)

**2026-07-07 — relay-due discharge SHIPPED**, the omission-side complement of
verify-relay: capability-2's second half (a relay boundary has two sides —
distortion and silence — and until tonight only distortion was enforced; the
live proving run had `notebook-status` compute `passed` and the agent never
relayed it, so the human never saw the verdict). `notebook-status` now journals
a relay-due MARKER on a TERMINAL verdict only — `passed` (the gate predicate
holds) or `failed` (a sign-off drift-revoked to `signed_stale`); the ordinary
in-loop `unsigned` mix sets NO marker (D8 applied to gates: marking everything
relay-due recreates alarm fatigue inside the enforcement). The marker rides
the same notebook journal as a new block class
(`notebook-relay-due`, `resolved={record_kind: "notebook-status", audit_id,
key_tokens: [state word, module sha12], created_at}`, deduplicated on the key
tokens so the op stays idempotent), excluded from the attestation reduction
exactly like render receipts. The relay-audit Stop hook's SAME stop runs the
discharge pass: any key token present in the final assistant text (plain
substring, case-insensitive) appends a `notebook-relay-discharge` record (the
marker key + `discharged_at`; the marker itself is never mutated — append-only
store); all tokens absent blocks the stop ONCE, verbatim-ready ("unrelayed
terminal state: notebook-status = <state> @ <sha12> — relay it verbatim before
closing."). Three pinned safety properties, each with a fires-AND-passes test:
block-once (the sibling guards' `stop_hook_active` seam, reused exactly — and
a forced continuation still records discharges, so a corrected relay closes
its own obligation), fail-open (ANY exception in marker load/parse/check lets
the stop proceed — the Option-3 failure class), narrow set (non-terminal runs
journal nothing). The skill prose gained the belt to the gate's suspenders
(the close-the-loop sentence, pinned by
`tests/contracts/test_notebook_relay_due_guidance.py`).

## Future work — audit-handoff: mechanize the audit→interview bridge (NOTED 2026-07-09)

Run #11: after an audit passes, the submit interview re-derives facts the
audit flow already holds, and the interim fix was a PROSE mapping in
`/new-experiment-hpc` step 4 — load-bearing prose, the rot class. The proper
seat is a deterministic projection verb (`audit-handoff` or a
`notebook-status` extension) that emits a draft `InterviewSpec` from durable
records: entry point + `audited_source` from the audit config,
`summary_artifact` CANDIDATES by AST-scanning the source's `$HPC_RESULT_DIR`
writes (detected-and-disclosed, never invented; caller confirms), `goal` and
the task axes from journaled elicitations — which requires the audit OPEN to
journal the intent/compute-shape utterances it already elicits (today they
live only in chat). The slash then says one non-load-bearing line: "run
audit-handoff, confirm its draft, pass it to the interview."

## Future work — run #11 mechanization queue (NOTED 2026-07-09, post-deadline)

Three prose rules from run #11 with real code seats, ranked value-per-effort
(user-endorsed for after the thesis deadline; see also the audit-handoff note
above and E-render in mcp-elicitation.md):

1. **Dirty-worktree disclosure at S1** (smallest; submit-side). "Commit
   before relaunch — uncommitted fixes are invisible to provenance" is
   prose-only today: `dirty` detection exists in `audit-preflight`
   (template-clean) and `verify_canary`, but nothing at submit resolve
   discloses a dirty experiment repo. Add a disclosure line to the S1 brief —
   NEVER a blocker (hacking dirty is legitimate; invisible-dirty is the bug).
2. **Sign-off echo detection** (hook-side). The "never compose the sign-off
   utterance" ban (skill invariant, 2026-07-09) is unenforced conduct prose;
   the relay-audit Stop hook already reads transcript + journal (rule 10), so
   it can flag a journaled `notebook-sign-off` `response` matching a prior
   ASSISTANT-authored line — laundered authorship. Complement of the F-R
   number-word class: F-R catches the model restating rejected content; this
   catches the human restating model-drafted attestation.
3. **Render relay-due markers** (medium; schema regen tail). "A link is not a
   relay" is prose; the Amendment-3 relay-due pattern (journaled marker only
   the relayed state word discharges) already covers `notebook-status`
   verdicts — extend it to `notebook-audit-view`: a per-section marker
   discharged by that section's `sha12` appearing in the turn, giving render
   relay the same omission-side enforcement the verdict relay got.

Stays prose, correctly: "the pipeline is the plan — no plan-mode freestyle"
(conduct with no code seat; observe/judge/route jurisdiction — a guard the
LLM itself satisfies is not a guard).

Addendum (same day): **4. `chunked_series` task-generator kind** (core;
submit-side). Run #11's bucket×chunk fan-out had no code seat for
series-chunking bounds: `data_axis_hint: bounded_halo` is a CLASSIFICATION
hint, not a materializer, so the agent (correctly) hand-scripted 800
`enumerated` items — and the interview cross-checks only the COUNT, not the
bounds arithmetic. An off-by-one in halo/last-chunk-end sails through. Add a
generator kind (`{series_length, chunks, halo}` → code-emitted per-task
bounds, property-tested) so the enumeration script disappears and the math
gets a test seat.

Addendum 2 (same day): **5. Decision-state claims join the relay-audit
corpus** (hook-side, pairs with item 2). Run #11 live instance: the agent
told the human "your y is revoked and nothing has advanced" with NO journal
record of the revocation — it was only narrated retroactively inside the NEXT
greenlight's evidence_digest. The state happened to be true (no scheduler
job_ids; only the speculative canary was in flight), but the claim was
unbacked at utterance time — rule 10's disease, outside rule 10's corpus
(numbers + section/run states). Extension: assertions of decision events
("revoked", "greenlit", "superseded", "journaled") must match a journal
record, and a revocation is a NUDGE — its own append-only record BEFORE the
claim, never a narration inside a later record.

Addendum 3 (same night): **6. Deploy delta on rsync-less hosts** (transport).
Run #11 live: a native-Windows host (no rsync on PATH) silently degraded to
the tar full-copy fallback and re-shipped 8.4 GB to CARC over a ~1 MB/s VPN
(hours) when >95% of the tree was already remote (17.8 GB from prior
campaigns). Two parts: (a) DISCLOSURE — the fallback must say "no rsync →
no delta → N MB will re-ship" at deploy start (the payload-size WARN exists;
the cause line does not); (b) MANIFEST DELTA — the tar path can delta without
rsync using the data-trace content-hash atoms: remote hashes its tree, local
tars only mismatched files into the F-G stage dir. Windows rsync installs
(MSYS/cwRsync) stay out of scope — the agent-blind-ssh / path-translation
class killed ControlMaster here already.

Addendum 4 (morning after): **7. Pre-deploy local smoke of task 0** (submit).
Run #11: a units bug (executor train_window in DAYS; 500 days = 24,000 bars >
every 2,425-bar chunk) survived the audit (human-signed — semantics are the
human's), the interview, and S1 validation, and was first caught by the REMOTE
canary — after an hours-long 8.4 GB staging. It would have crashed a LOCAL
task-0 dry-run in seconds. Wire the existing `ops/validate/dry_run_local.py`
seam into the submit flow as a bounded pre-deploy smoke (S1/S2 seat,
disclosure-or-refusal before transport ever runs). Core never interprets the
failure — it relays the executor's own crash.

**8. Overnight mode** (submit/campaign; user-requested). A journaled standing
consent for named boundaries while the human sleeps. Four pins from the live
night: (a) the consent is the human's own typed utterance accepting fallout,
journaled as its own record; (b) it binds to SPEC IDENTITY — run #11's gate
correctly refused to carry a pre-y across a cmd_sha change (regenerated grid),
and overnight mode must keep exactly that: consent dies on spec change;
(c) hard caps ride the record (budget / walltime / expires-at-morning);
(d) everything consumed under it is disclosed in a morning brief. This
formalizes the ad-hoc pre-y pattern instead of leaving it to per-night prose.

Amendment to item 8 (same morning): the live night exposed the missing half —
the canary FAILED overnight and sat undetected until the human woke and asked.
Two additions: (a) **the watch rule**: the agent armed a local-log Monitor on
a CLUSTER job — structurally blind (wrong machine); the submit/campaign skills
must name `status-watch` as the ONLY sanctioned watch for cluster state, and
a hand-rolled log tail is the improvisation class. (b) **the notification
leg**: relay-due honesty fires at the NEXT TURN, but overnight there is no
next turn — standing consent (item 8) must pair with push-on-terminal/anomaly
(the harness push capability, negotiated via harness-capabilities), else
"overnight mode" is just "morning surprise mode". Disclosure latency is part
of the fallout the consent record claims to accept — record it in the morning
brief (failed_at vs surfaced_at).

Addendum 5: **9. dir-digest + the context-budget rule** (user-requested:
"cut latency on stuff like ls-ing huge log directories"). Two parts:
(a) **dir-digest verb** — generalize `worker-log-digest` (one local file) to
directories: a bounded code-computed digest {file count, total size, newest N
by mtime, failure-marker hits across files, name-pattern groups}, never a
listing. Crucially REMOTE-capable: computed cluster-side by the deployed
runtime and shipped back as numbers — an 800-log dir over the VPN becomes ten
lines, not 800 filenames. An 800-task fleet makes every per-file surface a
directory problem.
(b) **the context-budget norm** (harness-contract.md) — the rule (a)
instantiates: an agent-visible payload is bounded; large content rides disk
by reference (path + sha + code digest); enforcement = a contract test
capping agent_facing result sizes. The run-#11 evidence: transcript bloat
was the "waiting for api response" latency, and ls-output is its next feeder.

Addendum 6: **10. The no-black-box contract** (user-requested: mechanized ops
must be observable while running — "heartbeat, tail output"). F-N generalized
from slot waits to EVERY op that can exceed ~10s: (a) each long op writes
progress lines to a tail-able well-known file (`.hpc/_progress/<op>-<id>.log`
or the existing worker log) — transfers report bytes done/total (tonight's
8.4 GB deploy was silent for its whole duration), reconcile sweeps report
n/N, snapshot reports its phase; (b) over MCP, ops additionally emit protocol
progress notifications (progressToken) where the client supports them — a
typed tool call must not be a black box until it returns; (c) silence >60s
from a running op is a REGRESSION by contract (the F-N standard), testable.

**11. Stale in-flight closure** (state hygiene; same evidence). 35 ebm_resid
runs died with the CARC account revocation and still read `in_flight` weeks
later — every unscoped surface walks the phantoms, and any per-run cluster
touch pays 35 × SSH × safe-interval ("status-snapshot is taking forever").
Remedy shape: a bulk reconcile seat — ONE batch-status qstat, then
scheduler-unknown non-terminal records close per the existing reconcile
classification (abandoned/completed_unknown), never one-by-one SSH. Candidate
home: doctor (it already owns the dead-worker scan) or a snapshot
`reconcile_all` arm.

Addendum 7: **12. Declared-but-dark elicitation clients** (mcp server). Run
#11: append-decision "hung" — all journal locks probed FREE; the wait was the
elicitation popup, capability-declared by the client but (apparently) never
rendered, so the refusal became a silent 300s stall. Two legs: (a) ADAPTIVE
DEGRADATION — an elicitation that times out undisplayed flips the session to
the hook path for subsequent refusals (re-probe next session; the capability
declaration is a claim, not a proof); (b) the wait itself joins the item-10
no-black-box contract — "waiting on human elicitation, Ns remaining" must be
visible somewhere tail-able, never dead air inside a tool call.

Addendum 8: **13. spec_hint completeness contract** (block chain; testable).
Run #11: the demo hand-authored a submit-s3 spec and bounced on a missing
required `monitor` property, then burned ~5 describe|grep round-trips (6s CLI
cold-start each) reverse-engineering MonitorFlowSpec. Two layers: the CONDUCT
fix is block-drive (the driver composes the successor spec — hand-authoring
is the corruption class it kills) and MCP tool schemas (already in-session,
zero describe calls). The CORE contract: for every edge in the block_chain
successor table, `spec_hint` ∪ the successor schema's defaults MUST validate
against that schema — one parametrized test over SUCCESSORS; a hint that
bounces off its own successor's validator is a driver bug, not an agent task.
