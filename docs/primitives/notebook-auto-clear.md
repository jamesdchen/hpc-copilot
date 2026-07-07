---
name: notebook-auto-clear
verb: mutate
side_effects:
- file_write: <experiment>/.hpc/notebooks/<audit_id>.decisions.jsonl
idempotent: true
idempotency_key: audit_id
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent notebook-auto-clear --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.notebook.auto_clear_op.notebook_auto_clear
---
# notebook-auto-clear

Journal **CODE auto-clear** attestations for the template-inherited, clean
sections of an audited source `.py` — the machine mirror of a human
`notebook-sign-off`. For every section the D-attention tiering deems
`auto_cleared` and that is not already cleared at its current hash, the verb
appends a `notebook-auto-clear` record (block `notebook-auto-clear`,
`response="auto_cleared"`, `attestor:"code"`, bound to the recomputed section
hash) to the `audit_id` decision journal. This is the only agent-facing writer of
those records — without it, template-inherited untouched sections could never pass
the graduation gate (`docs/design/notebook-audit.md` D-attention + D5).

## Un-fakeability

The verb **recomputes everything server-side**. It runs the `notebook-lint` rules
in-process and builds the D-attention view itself; it accepts **no**
caller-supplied lint findings and **no** tier claims. A caller passing empty
findings therefore cannot launder a flagged or modified section into
`auto_cleared` — the tier is recomputed from the freshly-parsed source + the
freshly-recomputed lint. The record is then bound through the one attestation
kernel against the recomputed section sha (`record_auto_clear` →
`attestation.bind`), so a machine clearance can no more assert a sha into
existence than a human sign-off can (D5 lock 2). The only caller inputs are
paths / ids / roots and an opaque forward-compat receipt.

## Inputs

A `NotebookAutoClearSpec` (`hpc_agent._wire.actions.notebook_auto_clear`):

- `audit_id` (string, required) — the notebook decision-journal scope id the
  auto-clear records are appended to (journal lives at
  `.hpc/notebooks/<audit_id>.decisions.jsonl`).
- `source` (string, required) — experiment-relative path to the audited source
  `.py` (jupytext percent format). Section shas are recomputed fresh on every
  call.
- `template` (string, required) — experiment-relative path to the template `.py`.
  A section auto-clears only when it is byte-identical (inherited) to its template
  section.
- `input_roots` (list of strings, default `[]`) — **opaque** data-path roots the
  server-side lint recompute tests path literals against. A section with a missing
  literal is flagged and therefore **not** auto-cleared.
- `source_roots` (list of strings, default `[]`) — **opaque** import roots the
  server-side lint recompute resolves imports under.
- `receipt` (object, optional) — **opaque** v1.5 execution receipt
  `{slug: {output_sha, error}}`. `error is False` greens that section's declared
  assertions; absent a receipt, a section *with* assertions is not green and stays
  `human_required`.

## Outputs

`data` is a `NotebookAutoClearResult`:

```
{
  "audit_id": "<id>",
  "cleared": [
    {"section": "<slug>", "section_sha": "<64-hex>", "view_sha": "<64-hex>"}
  ],
  "skipped": [
    {"section": "<slug>", "reason": "human_required | already-current"}
  ]
}
```

- **cleared** — sections a fresh CODE auto-clear record was journaled for, in
  source order.
- **skipped** — every other source section: `human_required` (a modified,
  lint-flagged, or ungreen-assertion section the code may never clear — only a
  human sign-off can), or `already-current` (already cleared-current in the
  journal — an auto-clear or a human sign-off at this hash — so a re-run appends
  nothing).

## Errors

- `spec_invalid` — an unreadable `source`/`template` path (naming which), a
  malformed percent-format module (a bad, duplicate, or misplaced
  `# hpc-audit-section:` marker — the parser's boundary guards), or a section sha
  that fails the recompute bind. Not retry-safe; fix the path or the source.

## Idempotency

Append-only but **idempotent by construction** (key `audit_id`): before appending,
each `auto_cleared` candidate is reduced against the existing journal — a section
already current (an auto-clear **or** a human sign-off at this hash) is skipped
`already-current`, so a re-run at unchanged hashes appends nothing. A section whose
prior auto-clear went stale (its source moved) reduces to `unsigned` and is
re-cleared at the **new** hash with a **new** record — never a mutation of the old
one (the journal is append-only; the newest valid record wins).

## Usage

```
hpc-agent notebook-auto-clear --spec spec.json --experiment-dir .
```

where `spec.json` is `{"audit_id": "<id>", "source": "<py relpath>", "template":
"<py relpath>", "input_roots": [...], "source_roots": [...]}`. Read the result of a
pass back with `notebook-status`; human sign-offs for the `human_required` sections
are appended out-of-band via `append-decision` (`block=notebook-sign-off`).
