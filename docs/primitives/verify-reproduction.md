---
name: verify-reproduction
verb: query
side_effects:
- filesystem: <experiment>/_aggregated/<repro_run_id>/reproduction_receipts.jsonl
    (append-only)
idempotent: false
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent verify-reproduction --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.verify_reproduction.verify_reproduction
---
# verify-reproduction

Compare a reproduction run's reduced metrics against the original it reproduces,
under a caller-owned tolerance, and append a durable receipt. This is the
COMPARISON half of the reproduction receipt
([`docs/design/reproduction-receipt.md`](../design/reproduction-receipt.md)) — it
answers one honest question, *did the numbers reproduce within a tolerance the
caller owns?*, and records the verdict where it survives a journal wipe.

The comparator carries **no metric vocabulary**. It never names a metric, never
privileges one (`accuracy` is not special, `loss` is not special), never picks a
tolerance, and has no per-metric default. It compares opaque numbers; naming and
judging are the human's job above core. `n_samples` compares exactly like any
other number — there is no metric-name special-casing anywhere.

A mismatch or an incomparable is a **successful run** (exit-0,
`needs_decision=True`): a discovered nondeterminism is the feature working, not
an error. The verb raises only when the pair is not a genuine reproduction or a
run's identity sidecar is missing.

## Inputs

- `original_run_id` (string) — the ORIGINAL run being reproduced.
- `repro_run_id` (string) — the reproduction run. Its sidecar `reproduces` field
  MUST name `original_run_id`, or the verb refuses.
- `tolerance` (object, optional) — caller-owned tolerance. Absent (or present
  with every bound absent) means an **exact** comparison:
  - `default_abs_tol` (float ≥ 0, optional) — absolute tolerance applied to
    every numeric key lacking a `per_key` override.
  - `default_rel_tol` (float ≥ 0, optional) — relative tolerance
    (`|orig-repro| / max(|orig|,|repro|)`) applied likewise.
  - `per_key` (object, optional) — `{metric_key: {abs_tol?, rel_tol?}}`
    overrides. A `per_key` entry FULLY replaces the default for that key. The
    key is the flattened metric key (see the ladder below).

## The artifact ladder

Each run's metrics are loaded via the same pure reducer the aggregate flow uses
— never a re-implementation:

1. `_aggregated/<run_id>/metrics_aggregate.json` — the cluster-final /
   default-path aggregate; its `aggregated_metrics` block is read directly (raw
   values preserved, numeric and non-numeric alike).
2. fallback — `reduce_partials` over the already-pulled
   `_aggregated/<run_id>/_combiner/` wave files.
3. else that side is `incomparable`, with the reason naming the missing
   artifact.

Both rungs yield the reducer's `{grid_key: {metric: value}}` shape, which is
flattened one uniform way (recursing into dict values, joining keys with `.`) to
scalar leaves — so a single-grid-point run's key is `<grid>.<metric>`. Flattening
never reduces or drops a value, so the comparator sees non-numeric leaves too.

## Comparison rules

Per key, over the union of both sides:

- key present on **one side only** → `incomparable`.
- numeric vs numeric → tolerance compare (exact `==` when no tolerance is
  supplied). A **NaN** on either side → `incomparable`, never a raw `!=`.
- non-numeric (either side) → equality only. A tolerance **supplied** for a
  non-numeric key → `incomparable` for that key.

Overall fold: any `mismatch` → `mismatch`; else any `incomparable` →
`incomparable`; else `match`.

## Outputs

`data` is a `VerifyReproductionResult`:

```
{
  "stage_reached": "match" | "mismatch" | "incomparable",
  "needs_decision": <bool>,          // True for mismatch/incomparable
  "reason": "reproduction verdict: <overall> — N matched, M mismatched, K incomparable of T metric keys",
  "receipt": { ... },                // the record appended to the ledger (below)
  "receipt_path": "<abs path to reproduction_receipts.jsonl>"
}
```

## The receipt

One JSON line is appended (append-only, `flock` + `fsync`, no dedup — each
verification is its own event) to
`_aggregated/<repro_run_id>/reproduction_receipts.jsonl`:

```
{
  "ts": "<iso-8601 utc>",
  "schema_version": 1,
  "original": {run_id, cmd_sha, tasks_py_sha, env_hash, data_sha, cluster, hpc_agent_version, submitted_at},
  "repro":    {run_id, cmd_sha, tasks_py_sha, env_hash, data_sha, cluster, hpc_agent_version, submitted_at},
  "tolerance_spec": <verbatim echo of the caller's tolerance, or null>,
  "per_key": [{key, original, repro, abs_diff, rel_diff, verdict, tolerance_applied}, ...],
  "overall": "match" | "mismatch" | "incomparable",
  "sources": {original_artifact, repro_artifact}
}
```

Each run's identity block is lifted **verbatim** off its sidecar — never
re-derived. The receipt lives experiment-local, beside the metrics it verdicts,
so it survives a journal wipe (a reproduction verdict is a durable scientific
record, not transient orchestration state).

## Errors

- `spec_invalid` — the reproduction run's sidecar `reproduces` field does not
  name `original_run_id` (not a genuine reproduction pair), or either run's
  identity sidecar is missing under `.hpc/runs/`.

## Idempotency

**Not idempotent** — the verdict is deterministic, but each call appends a new
receipt line (append-only, no dedup). A re-verification adds a second line so the
full history of every reproduction check survives.

## Notes

- v1 verifies the **full re-run**. A partial reproduction (a sampled subset of
  task ids) compares per-task, never pooled-vs-subset — that is a severable,
  deferred task.
- A mismatch is a FINDING, never an error. The human above reads it and decides
  whether it is a bug, a nondeterminism, or environment decay — core does not
  judge which.
