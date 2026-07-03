---
name: read-decisions
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent read-decisions --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.decision.journal.read_decisions
---
## Purpose

Read a run's or campaign's **decision journal** — the append-ordered `y`/nudge audit trail written by `append-decision` (design §2). The decision record, not the chat scroll, is the source of truth for why a run took the shape it did; this is how a fresh-context agent, a restarted session, or a human reconstructs that "why".

Takes a `ReadDecisionsInput` JSON spec (`scope_kind` + `scope_id`); returns every persisted record, oldest first.

## Inputs

- `scope_kind` (`"run"` | `"campaign"`) — which store.
- `scope_id` (str) — the `run_id` or `campaign_id`; filesystem-safe.

## Outputs

`{"path": "<journal jsonl>", "records": [<DecisionRecord>...], "count": N}` — `records` is empty (`count` 0) for a scope with no recorded touchpoints. Each record carries the design §2 schema (see `append-decision` for the field-by-field rationale): `schema_version`, `ts`, `scope_kind`, `scope_id`, `block`, `evidence_digest`, `proposal`, `response`, `resolved`, `provenance`.

## Errors

- `spec_invalid` — unknown `scope_kind` or non-filesystem-safe `scope_id`.

## Idempotency

Pure query — no state mutation, safe to call any number of times.

## Notes

- **Order is chronological** (append order), so `records[-1]` is the most recent exchange and a nudge→…→`y` loop reads top-to-bottom as it happened.
- **Robust to a torn line.** A blank or individually-corrupt JSONL line is skipped with a warning rather than failing the whole read — one bad line never strands the rest of the audit trail.
- **Reading a fresh scope has no side effect** beyond ensuring the `.hpc/runs/` or `.hpc/campaigns/<id>/` directory exists (the same dir-materializing layout access the monitor tick log makes).
