---
name: scope-lock
verb: mutate
side_effects:
- file_write: <experiment>/.hpc/scopes/<tag>.decisions.jsonl
idempotent: true
idempotency_key: scope
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent scope-lock --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.decision.journal.scope_lock.scope_lock
---
# scope-lock

Lock a caller-tagged experiment **scope** — a filesystem-safe slug the framework
attaches NO vocabulary to (it is not "holdout" / "test" / "embargo"; those are
caller-owned semantics). Locking is the **safe direction**: it only ever
restricts, so — unlike the unlock — it carries no human-authorship bar and
routes straight through `state.scopes.record_lock`, appending a `lock` decision
to the scope's journal (`.hpc/scopes/<tag>.decisions.jsonl`,
`scope_kind="scope"`, `resolved.scope_action="lock"`).

Unlocking has **no verb here**: relaxing a restriction is a human act, journaled
through [`append-decision`](append-decision.md) with `block="scope-unlock"` and
`resolved.scope_action="unlock"`, where the scope-unlock authorship gate refuses
a bare `y`.

## Inputs

- `scope` (string, required) — the scope tag. Filesystem-safe slug
  (`^[A-Za-z0-9._-]+$`); shape is the only constraint, never a role vocabulary.
- `reason` (string, non-empty) — the free-text WHY, stored as the lock
  decision's `response`. A lock without a reason is not auditable.

## Outputs

`data` is a `ScopeLockResult`:

```
{
  "scope": "<tag>",
  "locked": true,
  "already_locked": <bool>,
  "path": "<experiment>/.hpc/scopes/<tag>.decisions.jsonl"
}
```

`already_locked` reports whether the scope was **already** locked before this
call — the honest signal for the idempotent-in-effect semantics below.

## Errors

- `spec_invalid` — a non-slug `scope` tag, or an empty `reason`.

## Idempotency

**Idempotent-in-effect, keyed on `scope`.** Re-locking an already-locked scope
appends a second `lock` record (the journal is an append-only audit trail) but
leaves the lock **state** unchanged; `already_locked=true` surfaces that the
re-lock was a no-op on state. Callers can re-issue a lock safely.

## Notes

- The lock **state** is decided newest-first: the most recent `lock`/`unlock`
  record wins (`state.scopes.is_scope_locked`). An unlock never erases the lock
  history — both records stay on disk.
- No statistic is ever consulted. The scope substrate records lock state and a
  ledger of *looks* (run identities), never what a look found.
- Read the state back with [`scope-status`](scope-status.md).
