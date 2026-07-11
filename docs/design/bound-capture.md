# Bound capture — attestation carried by the channel, not reconstructed from the stream

Status: **PLANNED** (banked 2026-07-11, run-#12 finding-10 design note
promoted; dispatch-ready for an Opus wave). Prereq reading:
`docs/design/mcp-elicitation.md` (D1–D6, E-render), `run12-findings.md`
findings 9/10, `state/utterances.py` module docstring.

## The problem this retires

The authorship gates infer attestation from an unstructured stream: the
UserPromptSubmit hook logs EVERY human prompt, and the T8 sign-off gate
reconstructs intent forensically — name the slug token-exactly, engage a
diff identifier, be non-bare, and (finding 10) post-date the signed render.
Each rule is the counterexample to the previous one (finding 9: the
agent-relayed response laundered; finding 10: a resume paste naming the
slugs false-passed and kept the popup from ever firing). Forensic
reconstruction is inherently a patch treadmill because the channel carries
no intent.

The clean invariant: **a sign-off should be captured at a surface that
knows what it signs.** The E4 elicitation popup is exactly that surface —
when it opens, the server holds the precise `(scope_kind, scope_id, block,
resolved-subject)` it is asking about. Journal the typed response BOUND to
that tuple and the gate's primary evidence becomes one exact lookup.
Findings 9 and 10 become impossible by construction: a chat prompt was
never captured *for* anything, so it can never be bound evidence.

## Settled decisions

1. **The binding is code-authored, server-side.** `_elicit_then_retry`
   derives it from its own dispatch arguments — the same code-selected
   identifiers `_render_elicitation_prompt` already uses (D5). The model
   never touches it. Same trust class as the render header; filesystem
   forgery stays out of scope (the honest-limit paragraph).
2. **Additive record schema, no version bump.** An utterance record MAY
   carry `bound`; hook-captured records never do. Readers already read
   defensively by shape.
3. **The chat hook can never write `bound`** (it knows no scopes). A bound
   record therefore implies the elicitation channel (or a conforming
   second harness — see Conformance seam).
4. **Three evidence tiers, strongest first**, per sign-off:
   - **BOUND** — an utterance whose `bound` matches this append's
     `(scope_kind, scope_id, block)` and subject `(audit_id, section,
     view_sha)` exactly. Bar: non-bare only (`_is_bare_ack` still refuses
     a typed "y" — a deliberate statement is still required); the naming /
     diff-token / temporal legs are SUPERSEDED — the binding is the naming,
     and the popup displayed the render digest being signed.
   - **FORENSIC** — the chat-hook fallback, exactly today's rules (slug
     naming + diff engagement + temporal bind to the render mtime). Kept,
     and honestly labeled the weaker tier.
   - **FRICTION** — no log at all: the non-bare, slug-naming response
     (v1 behavior, byte-identical).
5. **Exact-match only.** A bound record never feeds the forensic tier for
   any other section/view; a re-rendered view (new `view_sha`) needs a new
   popup. No partial credit.
6. **v1 scope: the notebook sign-off gate only.** The `bound` shape is
   block-generic so scope-unlock and registration can adopt it later
   without schema change (they are all `append-decision` blocks), but v1
   changes one gate. (Open ruling c.)

## Record shape

```json
{
  "ts": "2026-07-11T04:12:09+00:00",
  "sha256": "<full-text digest>",
  "text": "<typed sign-off, size-capped>",
  "bound": {
    "channel": "elicitation",
    "scope_kind": "notebook",
    "scope_id": "causal_tune_linear",
    "block": "notebook-sign-off",
    "subject": {
      "audit_id": "causal_tune_linear",
      "section": "feature-construction",
      "view_sha": "8955b30903e6…"
    }
  }
}
```

`subject` is the code-copied identifying subset of the refusing call's
`resolved` (for a notebook sign-off: `audit_id`, `section`, `view_sha`).
Opaque to `state/utterances.py` — it stores and returns; only gates match.

## Waves (file-disjoint)

| Wave | Files | Change | Tests |
|---|---|---|---|
| 1 | `state/utterances.py` + `tests/state/test_utterances.py` | `append_utterance(..., bound: dict[str, Any] \| None = None)`; stored verbatim when given; absent key otherwise. Reader unchanged. | round-trips `bound`; absent stays absent; actor-suffixed log unaffected |
| 2 | `_kernel/extension/mcp_server.py` + `tests/test_mcp_elicitation_firing.py` | `_elicit_then_retry` builds the binding from `arguments["spec"]` (scope_kind/scope_id/block + the notebook subject keys when present) and passes it to `append_utterance`. Poison rule: values are COPIED identifiers only, never model prose (mirror `_render_elicitation_prompt`'s selection). | the accept-typed flow's logged record carries the exact binding; a non-notebook block binds without `subject.section` |
| 3 | `ops/decision/journal.py` + `tests/ops/test_decision_journal_primitives.py` | BOUND tier ahead of the forensic tier in the T8 gate: candidates = bound records matching the tuple + subject; on ≥1 non-bare match, accept (skip naming/engagement/temporal); else fall through to today's forensic tier unchanged. | bound match passes with zero slug tokens in text; typed bare "y" in a bound record still refused; binding for a DIFFERENT section/view falls through and forensic rules apply; hook-tier behavior byte-identical when no bound records exist |
| 4 | `docs/design/mcp-elicitation.md` (D-section amendment), `docs/design/notebook-audit.md` drift log, `harness-contract.md` addendum, `hpc-notebook-audit/SKILL.md` one line | record the tier order + the conformance seam; skill prose stops implying the chat text is the primary evidence | doc-status lints only |

Wave order 1 → 2 → 3 (schema before writer before reader); wave 4 rides
last. Regen: none (no `_wire` model or `@primitive` change; the utterance
store is not a wire surface).

## Conformance seam (anti-vendor-lockout)

A second conforming harness that renders sign-off cells
(`hpc-agent-notebook-render` `ingest-signoffs`) also knows exactly what a
typed cell signs — it SHOULD write bound records through the same
`append_utterance(bound=…)` API instead of bare text. Harness-contract
addendum: "a harness that captures a sign-off at a view-aware surface
records the binding; a harness that only mirrors prompts records bare
text and its sign-offs ride the forensic tier." Capability-honest, no new
verb.

## Failure-mode pass (pre-verified)

- **Re-rendered view between popup and append**: binding carries the old
  `view_sha`; the append for the new view finds no bound match → popup
  re-fires for the new view. Correct (the human signs what they saw).
- **Replayed bound record** (second append, same section+view): matches
  again — an append-only journal records a second identical attestation;
  harmless, same class as re-signing today.
- **Nested/suppressed elicitation, dark channel, decline, timeout**: all
  upstream of capture — no record is written, tiers unchanged.
- **Multi-human (MH4)**: the elicitation handler appends via the same
  actor plumbing as today; a scoped read filters bound records exactly as
  it filters bare ones. No new identity semantics.
- **Old wheels reading new logs**: unknown `bound` key is ignored by the
  defensive readers; forensic tier still applies to the record's text.

## Open rulings (defaults chosen; user may override at dispatch)

- **(a) Bound-tier bar** — DEFAULT: non-bare only. Alternative: also keep
  the diff-token engagement requirement inside the popup text. Default
  rationale: the popup displays the digest of the exact diff being signed;
  demanding token echo re-imports the forensic treadmill into the channel
  that was built to end it.
- **(b) Should FORENSIC-tier chat sign-offs eventually require an intent
  token ("sign …")** — DEFAULT: no change now; revisit with philosophy-
  audit axis B14 evidence.
- **(c) Unlock/registration adopt bound capture** — DEFAULT: post-v1, one
  gate per wave, after the T8 tier has live evidence.
