---
name: notebook-draft
verb: mutate
side_effects:
- file_write: <experiment>/.hpc/notebooks/<audit_id>.decisions.jsonl
idempotent: false
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent notebook-draft --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.notebook.draft_op.notebook_draft
---
# notebook-draft

Journal a **CODE draft attestation** for one section of an audited source `.py` —
the drafter-attribution seam of the multi-human substrate
(`docs/design/multi-human.md` MH5). It records the actor whose **session drove the
drafting** of a section as that section's **author**, at draft time: the LLM is
transport and the session-owner is the author (the model has no standing of its
own, exactly as it has none in the utterance log). The reviewer≠author gate (MH6)
later resolves the section's author from the newest draft attestation that is
still fresh at the current section hash, so a self-review (the drafter signing
their own section) can be detected and refused.

The verb appends a `notebook-draft` record (block `notebook-draft`,
`response="drafted"`, `attestor:"code"`, `subject_kind="notebook-draft"`,
bound to the recomputed section hash) to the `audit_id` decision journal. It is
not a clearance and not a sign-off — it carries the honest mechanical response
`"drafted"`, never a human-ack token.

## No actor field on the wire (the enforcement row)

The spec carries `{audit_id, source, section}` and **nothing that names an
actor**. The drafting actor is resolved **server-side** from the session
environment (`HPC_ACTOR`, via `hpc_agent.infra.env_flags.env_actor`) and validated
against the interview's declared `actors.ids` — it is **never** a caller-suppliable
field. An agent-suppliable actor would let the model choose its own identity; the
actor must arrive from outside the model's tool surface, exactly like the
utterance text it attributes. This is HARNESS-ASSERTED attribution: core compares
the slug by identity, never verifies who set it. The resolved actor is echoed on
the result (`actor`) as a server-computed output only.

### Actor resolution — the three outcomes

- **More than one declared actor and no session actor resolves → refused
  (`spec_invalid`).** An anonymous draft in a declared-multi-actor experiment is
  the laundering channel (draft as nobody, then self-review undetectably), so it
  is loud, never skipped. The refusal names the remedy: set `HPC_ACTOR` to one of
  the declared actors.
- **Zero or one declared actor → records with `attestor_id = None`** (or the
  resolved actor when the session is attributed and the slug is a declared id).
  Comparisons stay off, byte-identical to today's single-actor world. Zero declared
  actors always records `attestor_id = None` — there is nothing to attribute
  against.
- An `HPC_ACTOR` that is unset, an invalid slug, or **not** among the declared
  `actors.ids` resolves to `None` (which, under >1 declared actor, is the refusal
  trigger: an undeclared actor may not draft).

## Freshness by construction (the load-bearing constraint)

The verb parses the source **on disk** and binds the draft through the one
attestation kernel against the **freshly-parsed** section sha — the parse IS the
recompute (`record_draft` → `attestation.bind`). A caller can no more assert a
draft for a sha the `.py` does not currently carry than a human can assert a
sign-off (D5 lock 2). And because the record binds the section sha it was drafted
at, a **redraft** (which moves the sha) leaves the old draft record **stale** via
the one reducer — so authorship follows the CURRENT content with no state machine
(the D8 property). `read_draft_author` returns the author only when the newest
draft is current at the section's present hash.

## Inputs

A `NotebookDraftSpec` (`hpc_agent._wire.actions.notebook_draft`):

- `audit_id` (string, required) — the notebook decision-journal scope id the draft
  is appended to (journal at `.hpc/notebooks/<audit_id>.decisions.jsonl`).
- `source` (string, required) — experiment-relative path to the audited source
  `.py` (jupytext percent format). Parsed on disk; the draft binds the
  freshly-parsed section hash.
- `section` (string, required) — the section slug being attributed. Must exist in
  the parsed source (else a loud `spec_invalid` — there is no section sha to bind a
  draft against).

No actor field — see the enforcement row above.

## Outputs

`data` is a `NotebookDraftResult`:

```
{
  "audit_id": "<id>",
  "section": "<slug>",
  "section_sha": "<64-hex>",
  "actor": "<slug or null>"
}
```

- **section_sha** — the freshly-parsed sha the draft attestation was bound at.
- **actor** — the server-resolved drafting actor stamped as the attestation's
  `attestor_id` (opaque, harness-asserted, never verified), or `null` for an
  unattributed draft (zero/one declared actor). A server-computed output, never a
  wire input.

## Errors

- `spec_invalid` — an unreadable `source` path (naming it); a malformed
  percent-format module (a bad, duplicate, or misplaced `# hpc-audit-section:`
  marker — the parser's boundary guards); a `section` slug absent from the parsed
  source; or, when more than one actor is declared, a session with no resolvable
  declared actor. Not retry-safe; fix the path, the source, the slug, or the
  `HPC_ACTOR` configuration.

## Idempotency

Deliberately **not idempotent** (like `append-decision`): the journal is
append-only, so each call adds a fresh draft line. A re-draft at an unchanged hash
appends a new record — the newest valid draft wins on read (`read_draft_author`),
so retries are safe but not byte-idempotent.

## Usage

```
hpc-agent notebook-draft --spec spec.json --experiment-dir .
```

where `spec.json` is `{"audit_id": "<id>", "source": "<py relpath>", "section":
"<slug>"}`. The `hpc-notebook-audit` skill records a `notebook-draft` after each
accepted (re)draft, so the section carries a current author for the reviewer≠author
gate. The session's `HPC_ACTOR` supplies the attribution; it is never passed on the
wire.
