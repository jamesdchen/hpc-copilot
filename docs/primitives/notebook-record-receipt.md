---
name: notebook-record-receipt
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
  cli: hpc-agent notebook-record-receipt --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.notebook.record_receipt_op.notebook_record_receipt
---
# notebook-record-receipt

Journal **CODE render receipts** for the sections of an audited source `.py` — the
emitter's evidence that a section was rendered (executed) in the caller's
environment and whether its declared assertions errored. For every entry whose
slug exists in the freshly-parsed source, the verb appends a
`notebook-render-receipt` record (block `notebook-render-receipt`,
`response="rendered"`, `attestor:"code"`, bound to the recomputed section hash) to
the `audit_id` decision journal (`docs/design/notebook-audit.md` T10).

A render receipt is the execution evidence the D-attention tier's
**assertions-green** leg consumes: a section carrying declared `assert`s is not
`auto_cleared` until a receipt says its render did not error. `notebook-auto-clear`
reads these journaled receipts (sha-fresh only) — it never trusts a
caller-supplied receipt.

## Freshness by construction (the load-bearing constraint)

The verb parses the source **on disk** and binds each receipt through the one
attestation kernel against the **freshly-parsed** section sha — the parse IS the
recompute (`record_render_receipt` → `attestation.bind`). A receipt can therefore
only ever be recorded against the source as it currently sits on disk, and it
reads **stale** (greening nothing) the moment the section drifts. This is what
closes the v1 laundering hole, where `notebook-auto-clear` trusted an opaque
caller receipt `{slug: {error: False}}` with no execution evidence and no
freshness key.

## Inputs

A `NotebookRecordReceiptSpec` (`hpc_agent._wire.actions.notebook_record_receipt`):

- `audit_id` (string, required) — the notebook decision-journal scope id the
  receipts are appended to (journal at `.hpc/notebooks/<audit_id>.decisions.jsonl`).
- `source` (string, required) — experiment-relative path to the audited source
  `.py` (jupytext percent format). Parsed on disk; each receipt binds the
  freshly-parsed section hash.
- `entries` (object, required, non-empty) — map of section slug → render outcome
  `{output_sha, error}`. `output_sha` is an **opaque** caller hash of the rendered
  output (never parsed by core); `error` is a bool (`false` greens the section's
  assertions-green tier leg while the receipt is fresh; `true` never greens). One
  receipt is journaled per slug that exists in the parsed source.

## Outputs

`data` is a `NotebookRecordReceiptResult`:

```
{
  "audit_id": "<id>",
  "recorded": [
    {"section": "<slug>", "section_sha": "<64-hex>", "output_sha": "<str>", "error": false}
  ],
  "skipped": [
    {"section": "<slug>", "reason": "unknown-slug"}
  ]
}
```

- **recorded** — sections a render receipt was journaled for, in entry order.
- **skipped** — entries NOT journaled: `unknown-slug` (the entry named a section
  the parsed source does not contain — a stale or mistyped slug, so there is no
  section sha to bind against). Reported, never fatal — a mismatched entry never
  strands the receipts for the sections that DO exist.

## Errors

- `spec_invalid` — an unreadable `source` path (naming it), or a malformed
  percent-format module (a bad, duplicate, or misplaced `# hpc-audit-section:`
  marker — the parser's boundary guards). Not retry-safe; fix the path or the
  source.

## Idempotency

Deliberately **not idempotent** (like `append-decision`): the journal is
append-only, so each call adds a fresh receipt line per known slug. A re-record at
an unchanged hash appends a new record — the newest valid receipt wins on read
(`read_render_receipts`), so retries are safe but not byte-idempotent.

## Usage

```
hpc-agent notebook-record-receipt --spec spec.json --experiment-dir .
```

where `spec.json` is `{"audit_id": "<id>", "source": "<py relpath>", "entries":
{"<slug>": {"output_sha": "<hash>", "error": false}}}`. The caller's execution
contract runs the sections and emits the `{slug: {output_sha, error}}` map; this
verb journals it. Then `notebook-auto-clear` reads the fresh receipts and clears
the newly-green sections.
