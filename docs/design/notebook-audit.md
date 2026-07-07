# The notebook-audit substrate — design + implementation plan

**Status: v1 IMPLEMENTED (2026-07-08).** Core (T0–T9) + the skill
(`src/slash_commands/skills/hpc-notebook-audit/SKILL.md`) + three query
verbs (`notebook-lint`, `notebook-audit-view`, `notebook-status`) + one
mutate verb (`notebook-auto-clear` — see the drift log) are in the tree.
v1.5 (the jupytext export, render receipts, verify-relay hash claims)
remains scheduled. Cite `path::symbol`, never line numbers. Implementation
drift from this plan is recorded in the drift log at the end of this
document.

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
compete under it.

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
- **T8 tier-recompute boundary:** at append time the sign-off gate checks the
  statically-recomputable tier legs (diff classification,
  assertions-without-receipt) with `lint_findings=()`; a section made
  human-required solely by a lint flag is not distinguished at the gate.
  `view_sha` is validated present but never recomputed there — it is a
  provenance witness; the recompute lock is `section_sha`. No resolvable
  template → every section reads added → HUMAN_REQUIRED (conservative).
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

## Related, planned separately

The palatability projections the same review surfaced: the **run story** (a
code-rendered timeline of a run's journal trail — the decision journal's
interface sibling) and the **attention queue** (status-snapshot v2: fleet
overnight digest ordered by needs-your-verdict-first). Both pure
ordering/identity projections; natural siblings of T5's renderer posture.
