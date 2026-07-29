---
name: notebook-record
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
  cli: hpc-agent notebook-record --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.notebook.record_op.notebook_record
---
# notebook-record

The merged notebook-audit journaling verb — one `mutate` surface, two kinds,
dispatching on the spec's `kind` discriminator to the two seats that were
previously the standalone verbs `notebook-record-config` and
`notebook-record-receipt`. The seat implementations (and their design
rationale) stay in `ops/notebook/record_config_op.py` /
`ops/notebook/record_receipt_op.py`; this verb owns only the registration and
the dispatch. Both kinds append to the same journal
(`.hpc/notebooks/<audit_id>.decisions.jsonl`) and share the same trust
posture: the verb journals caller-side evidence, it never actuates.

## `kind: "config"` — the standalone audit's configuration seat

Run-#10 live finding (`docs/design/notebook-audit.md` Amendment 2): a
standalone audit — one that never opted in through the interview — ran
ROOTLESS-canonical: the lint recomputed with empty roots and the
template-mandated `source_roots` engine-drift binding was silently inactive.
This kind is the seat that records the configuration (`input_roots` /
`source_roots` / `attention_order` / `output_roots` — all OPAQUE relpath
strings), optionally carrying the audit-OPEN intent (`goal` + `task_axes`,
verbatim, never invented) that `audit-handoff` reads.

- The canonical read takes interview `audited_source` FIRST when present (the
  opt-in path owns the config; one source of truth), else this journaled
  record, else empty.
- **Refuses** (`spec_invalid`) when interview.json already carries
  `audited_source` for this `audit_id`, and when a config record already
  exists — the config is IMMUTABLE-PER-AUDIT (every view_sha and sign-off is
  downstream of it); superseding means a NEW `audit_id`.
- Recording into an audit that already has journal entries SUCCEEDS with a
  loud `warning`: every view_sha moves, prior sign-offs read stale (disclosed,
  never silent — drift-revocation is the kernel's job).

## `kind: "receipt"` — the emitter's sha-bound render receipts

The journaling surface for CODE render receipts (notebook-audit T10): evidence
that a section's source was RENDERED (executed) and whether its declared
assertions errored. Parses the source `.py` ON DISK and binds each receipt
through the attestation kernel against the FRESHLY-PARSED section sha — **the
parse IS the recompute**, never a caller-asserted sha — so a receipt can only
be recorded against current source and drifts stale the moment the section
moves. `notebook-auto-clear` reads these journaled receipts (sha-fresh only);
it never accepts an inline caller receipt.

- What this does NOT close: truthfulness. `output_sha` / `error` stay
  caller-attested per the D9 execution contract; the graduation consumers
  WEIGH that outcome, they do not re-derive it.
- Unknown slugs (an entry naming a section the parsed source lacks) are
  reported `skipped`, never fatal.
- Append-only: a re-record at an unchanged sha appends a fresh line (newest
  valid receipt wins on read) — retries are safe but not byte-idempotent,
  like `append-decision`.

## Inputs

A kind-discriminated `--spec` (see `notebook_record.input.json`): the config
kind takes the roots + optional intent fields; the receipt kind takes
`{audit_id, source, entries: {slug: {output_sha, error}}}`.

## Outputs

One shape per kind (the union is `notebook_record.output.json`): the config
kind echoes the recorded configuration plus the optional late-record
`warning`; the receipt kind returns `{audit_id, recorded, skipped}` with the
bound section shas.

## Errors

`spec_invalid` — the config-kind refusals above; an unreadable source path or
malformed percent-format module for the receipt kind; a spec that fails the
kind-discriminated schema.

## Idempotency

Not idempotent under either kind, honestly: a config retry after success is
itself refused (immutable-per-audit — the scaffold-template precedent); a
receipt retry appends a new line rather than being byte-identical.

## Notes

Both kinds are pure local read + journal append — no SSH. The
`/hpc-notebook-audit` skill drives both: the config seat at onboarding (and
via `references/interview-handoff.md` when the audit hands off to a submit
interview), the receipt seat when an assertion-bearing section needs its
execution receipt before `notebook-auto-clear` can green it. The optional
`hpc-agent-notebook-render` plugin journals receipts in one step
(`notebook-render --execute --record_receipts`).

**Schemas:** [`notebook_record.input.json`](../../src/hpc_agent/schemas/notebook_record.input.json),
[`notebook_record.output.json`](../../src/hpc_agent/schemas/notebook_record.output.json).
