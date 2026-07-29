# `append_jsonl_line` consumer classification (unit D-FSYNC deliverable)

Swarm finding 4: the shared append seam
(`src/hpc_agent/infra/io.py::append_jsonl_line`) has **16 core consumers**. Each
is classified **source-of-truth** (a record whose loss corrupts the audit /
lineage / determinism chain → durability is mandatory, fsync `OSError` must
raise) or **best-effort marker** (a `finally`-time / fail-open write whose
caller contract is never-raise → fsync `OSError` stays suppressed).

## The durability default (design decision)

`append_jsonl_line` gains `fsync_required: bool = True`. The default **raises**
on an fsync `OSError` — "no ack without durability". This makes every
source-of-truth consumer durable **by inheritance**, including the six under
`ops/` that unit D-FSYNC's file boundary does not touch. Best-effort markers
pass `fsync_required=False` to keep suppression.

This is the shared-seam change ruled AFFIRMATIVE 2026-07-16 (Δ-RULING-1): the
one-shot CLI now surfaces a source-of-truth fsync failure as `ok:false` —
"byte-identical modulo the shared durability fix".

Both best-effort markers (rows 15–16) live **outside** D-FSYNC's file boundary
and today route through the default-`True` seam, but each already wraps the call
fail-open at its own call site, so the never-raise caller contract is preserved
(on a genuine fsync `OSError` the marker line is dropped rather than
written-sans-fsync — acceptable for a best-effort marker). Passing
`fsync_required=False` for exact suppression is a one-line follow-up owned by the
units that own `ops/monitor/harvest_guard.py` and `infra/transport/_prune.py`.

## Table

| # | Consumer (site) | Ledger | Class | fsync | In D-FSYNC scope? |
|---|---|---|---|---|---|
| 1 | `state/decision_journal.py:255` (via `_append_jsonl_line`) | decision journal `<run>.decisions.jsonl` | source-of-truth | required (default) | YES (edited) |
| 2 | `state/decision_briefs.py:117` (via `_append_jsonl_line`) | decision briefs `<run>.briefs.jsonl` | source-of-truth | required (default) | YES (inherits) |
| 3 | `state/scopes.py:232` (via `_append_jsonl_line`) | scope look ledger `<tag>.looks.jsonl` | source-of-truth (lineage) | required (default) | YES (inherits) |
| 4 | `state/fingerprint_store.py:310` | determinism fingerprint samples | source-of-truth | required (default) | YES (inherits) |
| 5 | `state/data_trace.py:735` | per-task data trace | source-of-truth | required (default) | YES (inherits) |
| 6 | `state/data_trace.py:835` | data-trace store | source-of-truth | required (default) | YES (inherits) |
| 7 | `state/data_manifest.py:330` | data-manifest mint journal | source-of-truth | required (default) | YES (inherits) |
| 8 | `state/conformance_store.py:154` | conformance observation receipts | source-of-truth | required (default) | YES (inherits) |
| 9 | `ops/aggregate_flow.py:1467` | aggregate memo ledger | source-of-truth | required (default) | no (inherits default) |
| 10 | `ops/verify_reproduction.py:645` | reproduction match ledger | source-of-truth | required (default) | no (inherits default) |
| 11 | `ops/recover/heal_taxonomy.py:364` | heal anchor ledger | source-of-truth | required (default) | no (inherits default) |
| 12 | `ops/pack/init_op.py:309` | pack bind/receipt | source-of-truth | required (default) | no (inherits default) |
| 13 | `ops/pack/refresh_op.py:285` | pack refresh receipt | source-of-truth | required (default) | no (inherits default) |
| 14 | `ops/overnight.py:736` | overnight consumption ledger | source-of-truth | required (default) | no (inherits default) |
| 15 | `ops/monitor/harvest_guard.py:331` | guaranteed-harvest marker | **best-effort** (finally-time, fail-open wrapper) | ideally `False`; today default-`True` swallowed by wrapper | no (out of scope; follow-up) |
| 16 | `infra/transport/_prune.py:198` | deploy-prune timeline | **best-effort** (fail-open wrapper) | ideally `False`; today default-`True` swallowed by wrapper | no (out of scope; follow-up) |

**Tally:** 14 source-of-truth (durable by default) · 2 best-effort markers.

Out-of-core (not counted in the 16): the notebook-render example plugin's
between-cell observer, `examples/plugins/hpc-agent-notebook-render/.../_observe.py:219`
(transport trace, best-effort) — a separately-deployed package, out of the core
seam's contract.

## The other two seam changes (apply to every row)

- **Torn-line self-heal (Δ3 / state-concurrency F4):** inside the flock, before
  writing, the seam checks the file's last byte; a torn tail (no trailing `\n`,
  the shape a mid-append kill leaves) gets its boundary restored and is logged —
  isolated on its own line, never merged into the next record. One definition →
  the class is closed for **every** consumer and every killer.
- **First-append parent-dir fsync:** the append that creates a new ledger fsyncs
  the parent dir (best-effort) so a brand-new file's dirent is durable.

## request_id replay dedup (Δ2b) — decision journal only

The seam gains an optional `dedup_key=(field, value)`; the decision journal
passes `("request_id", request_id)` when a client mints one. A same-id
re-append, checked **under the append flock**, is a replay no-op (nothing
written, the original record returned) — closing the run-#2 duplicate-greenlight
class and the daemon deadline-abandon double-append class race-free. Standalone,
the `ops` `append-decision` leg sources `request_id` from
`provenance["request_id"]` (the no-schema-change channel on the frozen
`AppendDecisionInput`); the daemon RPC transport (D-CORE/D-CLIENT) will supply it
out-of-band later.
