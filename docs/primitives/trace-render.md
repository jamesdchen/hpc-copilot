---
name: trace-render
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent trace-render --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.trace_render_op.trace_render
---
# trace-render

Render one task's **data trace** as four deterministic markdown views under a
self-describing run/config header. A pure read (`docs/design/data-trace.md`,
Wave 3 / T5): it reads one task's stage receipts out of the canonical trace
store, joins the run/audit sidecar for a cold-reader header, and renders the
four projections over the records + the ONE atom-schema registry
(`hpc_agent.state.data_trace`). It never touches a frame, never runs SSH, never
mutates the store — derived state recomputed from disk on every call, so it can
never drift from a second source of truth.

The four views:

- **(a) row waterfall** — stage-by-stage `rows_in / dropped / rows_out` with the
  conservation pre-image (`expected = rows_in - dropped`); the generic
  row-conservation invariant flags render directly beneath.
- **(b) label-chain line** — each tracked label's value chain across stages (the
  units ledger, generalized); a broken chain surfaces as a continuity flag.
- **(c) feature lineage** — the `col_set` add/drop delta per stage plus a
  column → birth-stage map.
- **(d) sketch table** — `value_sketch` (min/mean/std/max + fixed q05/q50/q95)
  and `null_count` per column per stage.

## Trusted-display posture

The returned `render` is a **trusted-display** artifact: code renders it, the
agent relays it VERBATIM, and it carries **no verdict vocabulary** — the trace
SHOWS, the scientist concludes (the pointing doctrine applied to data;
grep-pinned by the never-judgment test). Flags render as the records' OWN
`{rule, detail}` text; core points at an atom delta, it never says a run is
right or otherwise. A null measurement renders as a bare `-`, never a
fabricated `0`.

## Inputs

A `TraceRenderSpec` (`hpc_agent._wire.queries.trace_render`) — **exactly one**
selector:

- `scope_kind` + `scope_id` (strings) — the DIRECT point lookup: read
  `.hpc/traces/<scope_kind>/<scope_id>/task-<task>.jsonl` verbatim
  (`scope_kind` is `run` / `audit` / `local`).
- `cmd_sha` (string) — the REFERENCE lookup: the newest run whose sidecar
  records this parameter identity, then its run-scope trace (Class B).
- `profile` (string) — the REFERENCE lookup by the sidecar's literal `profile`
  label: the newest run carrying it (latest-by-profile).

Plus `task` (int, default `0`) — which per-task trace file (single-task
local/audit runs use `task-0`) — and `markdown` (bool, default `true`).

## Outputs

A `TraceRenderResult`: the resolved `scope_kind` / `scope_id` / `task` /
`resolved_from` (`spec` | `cmd_sha` | `profile`), the `present` / `skipped`
absence disclosure, `stage_count`, the `trace_sha` fingerprint, the
self-describing `header`, the four structured views (`waterfall`,
`label_chains`, `feature_lineage` + `feature_births`, `sketch`), the full
`flags` list, and the `render` markdown string.

## Errors

- `spec_invalid` — a structurally invalid scope key (the store-path guard), or
  the wrong number of selectors (the spec validator).

## Idempotency

Idempotent by construction: a pure projection recomputed from the on-disk
records on every call. No store, no write, no attestation — `trace_sha` is a
FINGERPRINT over the records, not a claim about them.

## Notes

Absence is DATA, never an error. An unresolved reference lookup (no run matched)
or a scope with no recorded trace yields `present=false`, a `skipped`
disclosure ("no trace recorded for this scope"), and empty views — the honest
result a cold reader needs, never a raised exception.

The `cmd_sha` and `profile` lookups are the Class-B **reference/comprehension**
consumers (a human or drafting LLM reading a reference trace to learn what the
pipeline IS). Core stays agnostic to WHICH profile is the exemplar — the caller
names it; core joins the sidecar and renders.
