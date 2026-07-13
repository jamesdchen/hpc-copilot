---
name: scope-status
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent scope-status --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.decision.journal.scope_lock.scope_status
---
# scope-status

Report the lock + look state of a caller-tagged **scope**, or of every scope
under `.hpc/scopes/`. A **pure read** (no side effects, no SSH): it consults the
scope's decision journal for the lock state and the look ledger for the counts,
and interprets neither — no statistic is ever read.

## Inputs

- `scope` (string, optional) — the scope tag to report. Filesystem-safe slug.
  **Omit it** to report every scope with an on-disk store under `.hpc/scopes/`
  (a decision journal or a look ledger); a missing tree reports `{}`.

## Outputs

`data` is a `ScopeStatusResult` — a map keyed by scope tag:

```
{
  "scopes": {
    "<tag>": {
      "locked": <bool>,
      "looks": {"prior_looks": <int>, "distinct_lineages": <int>},
      "lock_history_len": <int>
    }
  }
}
```

- `locked` — the current lock state (newest `lock`/`unlock` record wins).
- `looks` — `prior_looks` (total looks recorded) and `distinct_lineages`
  (distinct lineage roots, collapsing a supersession chain of reruns to one
  experiment). Plain integers over IDENTITIES; no metric is consulted.
- `lock_history_len` — the number of `lock`/`unlock` records on the append-only
  journal (the lock history is never truncated).

## Errors

- `spec_invalid` — a non-slug `scope` tag.

## Idempotency

Pure read of on-disk state; no side effects. Not keyed on `scope` — it reflects
whatever is on disk at call time.

## Notes

- A *look* is a run whose results were reduced against the scope; the ledger
  stores identity (run_id, cmd_sha, lineage_root, reducer_block), never a metric.
- Lock/unlock the scope with [`scope-lock`](scope-lock.md) and (for the human
  unlock) [`append-decision`](append-decision.md).
