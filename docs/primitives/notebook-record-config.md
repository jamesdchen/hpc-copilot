---
name: notebook-record-config
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
  cli: hpc-agent notebook-record-config --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.notebook.record_config_op.notebook_record_config
---
# notebook-record-config

Record the **audit configuration** for a **standalone** notebook audit — one
that never opted in through interview.json's `audited_source` block — as a
journaled `notebook-audit-config` record in the audit's own decision journal
(`.hpc/notebooks/<audit_id>.decisions.jsonl`). The run-#10 finding
(`docs/design/notebook-audit.md` Amendment 2): without a seat for this config,
a standalone audit ran **rootless-canonical** — the canonical view recomputed
the lint with empty roots, so the template-mandated `source_roots` engine-drift
binding was silently inactive and executes-live flags fired against no roots.

## The recorded-config precedence (one source of truth)

`read_recorded_config` (the one canonical-config read every view recompute
routes through) resolves in this order:

1. interview.json's `audited_source` block matching the `audit_id` **wins**
   when present — the opt-in path owns the config (even a block predating the
   config fields wins, yielding the conservative defaults).
2. Else the **journaled** `notebook-audit-config` record this verb appends.
3. Else the conservative defaults (empty roots, source order) — exactly the
   pre-seat posture.

## Immutability + the late-record disclosure

- **Immutable per audit**: a second config record for the same `audit_id` is
  refused — every `view_sha` and sign-off is downstream of the config, so a
  mutable config would silently re-key the audit trail. To supersede a recorded
  config, start a **new `audit_id`** and record there.
- **Late record is disclosed, never silent**: recording a config into an audit
  that already has journal entries (sign-offs, auto-clears, receipts) succeeds,
  but the result carries a loud `warning` — the config enters every canonical
  view recompute, so **every `view_sha` moves** and prior sign-offs read stale
  against the new canonical view. That is correct behavior (drift = unsigned);
  re-run `notebook-audit-view` and re-sign what still matters.

## Inputs

A `NotebookRecordConfigSpec` (`hpc_agent._wire.actions.notebook_record_config`):

- `audit_id` (string, required) — the notebook decision-journal scope id the
  config record is appended to.
- `input_roots` (list of strings, required, may be empty) — **opaque** data-path
  roots the executes-live lint tests path literals against.
- `source_roots` (list of strings, required, may be empty) — **opaque** import
  roots the linked-sources lint resolves imports under (the engine-drift
  binding).
- `attention_order` (list of strings, optional) — section-slug presentation
  ordering (`null` = source order); participates in the module `view_sha` only.
- `output_roots` (list of strings, optional, default `[]`) — **opaque**
  write-target roots: a path literal under one is a declared output, exempt from
  the executes-live not-exists flag (reported in `declared_outputs`, never
  flagged).

All roots are opaque relpath strings — core never attaches a meaning to a root.

## Outputs

`data` is a `NotebookRecordConfigResult`:

```
{
  "audit_id": "<id>",
  "input_roots": ["data"],
  "source_roots": ["src"],
  "attention_order": null,
  "output_roots": ["results"],
  "warning": null
}
```

- an echo of the journaled configuration, plus
- **warning** — non-null iff the audit already had journal entries when the
  config was recorded (the late-record disclosure above).

## Errors

- `spec_invalid` —
  - interview.json already carries an `audited_source` block for this
    `audit_id` (the opt-in path owns the config; standalone recording is for
    audits with no block), or
  - a config record already exists for this `audit_id` (immutable-per-audit —
    supersede with a new `audit_id`), or
  - a bad `audit_id` scope (not a filesystem-safe slug).

  Not retry-safe; fix the spec (or mint a new `audit_id`).

## Idempotency

Deliberately **not idempotent** (the `notebook-scaffold-template` precedent):
the config is immutable per audit, so an immediate retry of a SUCCEEDED call is
itself refused (config already recorded) — honest, not retry-equivalent.

## Usage

```
hpc-agent notebook-record-config --spec spec.json --experiment-dir .
```

where `spec.json` is `{"audit_id": "<id>", "input_roots": ["data"],
"source_roots": ["src"], "output_roots": ["results"]}`. Record the config
FIRST — before the first `notebook-audit-view` — so every view of the
standalone audit is canonical against non-empty roots from the start and no
sign-off ever binds a rootless `view_sha`.
