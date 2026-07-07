# The notebook-audit substrate — design + implementation plan

**Status: PLANNED (2026-07-07), not yet implemented.** This document is the
durable hand-off of a full planning cycle (plan + two user-driven revisions);
the next session dispatches the implementation from it. Cite `path::symbol`,
never line numbers.

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

## The renderer plugin (the other half — PLAN NEXT SESSION)

`hpc-audit-render` in the plugin lane (plugins have their own CI job and dep
budget — Q4's answer): jupytext + nbclient; renders (py + template +
execution) → the audit notebook embedding T5's code-rendered views; collects
per-section human responses and routes them through append-decision (the
render is an input surface, NEVER the gate); emits the render receipt for
v1.5 freshness. Also in its scope: the render-determinism duties
(SOURCE_DATE_EPOCH, output normalization) core refused. Planning inputs: the
existing plugin infrastructure (see the `plugins (hpc-agent-github-actions)`
CI job), D-attention (the tier decides which sections even render a sign-off
prompt), and the harxhar templates as the first consumer.

## Boundary-drift flags (Q1 watch list)

executes-live must never grow a reader-function vocabulary (read_csv etc. —
that needs a Q2 assembly point / pack matcher); template slugs stay opaque
(content-meaning checks are pack territory); linked-sources judges import
ORIGIN IDENTITY only; marker syntax stays comment-only (jupytext metadata
would couple core to its grammar); the render receipt stays opaque
`{slug: sha}` — parsing an output crosses Q1; sign-off UX pressure to soften
the human-required tier is the feature working — soften only via richer
harness-captured utterances, never bare acks.

## Related, planned separately

The palatability projections the same review surfaced: the **run story** (a
code-rendered timeline of a run's journal trail — the decision journal's
interface sibling) and the **attention queue** (status-snapshot v2: fleet
overnight digest ordered by needs-your-verdict-first). Both pure
ordering/identity projections; natural siblings of T5's renderer posture.
