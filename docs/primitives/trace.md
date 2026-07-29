---
name: trace
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent trace [--spec <path>] [--experiment-dir <dir>] [--campaign-id <campaign_id>]
    [--run-id <run_id>] [--format <trace_format>]
  python: hpc_agent.ops.trace.trace
---
# trace

One trace verb, three modes (merged from the former standalone `trace-diff` /
`trace-render` registrations — the implementations and their data-trace design
pins stay in `ops/trace_diff_op.py` / `ops/trace_render_op.py`):

- **Lineage (default, no `--spec`)** — assemble a *derived* execution DAG for a
  campaign or a single run's lineage by joining the three trace surfaces
  hpc-agent already records: the per-run journal records (lifecycle state, wave
  verdicts, job ids), the per-run sidecars (the immutable submit-time
  `{code, data, env, params}` fingerprint), and the signable provenance
  manifest. The read-side complement to the OpenTelemetry sink: OTel streams
  the trace live; `trace` reconstructs it after the fact for replay, audit, or
  agent consumption.
- **`--spec {"mode": "render", ...}`** — render one task's data trace as the
  four deterministic markdown views (`docs/design/data-trace.md` T5): row
  waterfall with conservation flags, label-chain line, feature lineage, sketch
  table — under a self-describing run/config header. The render carries NO
  verdict vocabulary — the trace SHOWS, the scientist concludes; relay it
  verbatim. Absence is honest (`present`/`skipped`, never an error).
- **`--spec {"mode": "diff", ...}`** — overlay TWO traces and report, per stage
  and per atom, where their measurements diverge (data-trace Projection 5),
  highlighting the FIRST diverging `(stage, atom)`. Every comparison dispatches
  through the ONE semantics registry (`state/data_trace.py::comparison_for`).
  Differences are FACTS (`row_count rows 100 → 90`), never verdicts; tolerance
  is caller-owned (absent → exact).

All modes are read-only local reads — no SSH, no scheduler.

## Inputs

Lineage mode (args, no spec):

- `--campaign-id` (string) — trace every run tagged with this campaign_id.
  Mutually exclusive with `--run-id`.
- `--run-id` (string) — trace this run plus its transitive lineage (the
  `parent_run_ids` resubmit chain). Mutually exclusive with `--campaign-id`.
- `--format` (`dag` | `flat` | `dot`, default `dag`) — `dag` emits `run` and
  `wave` nodes plus `member` / `derived-from` / `contains` edges; `flat` emits
  the `run` nodes only; `dot` adds a rendered Graphviz `dot` string (pipe to
  `dot -Tsvg`).

Exactly one of `--campaign-id` / `--run-id` is required when no spec is given.

Projection modes (`--spec`, mode-discriminated — see `trace.input.json`):

- `mode: "render"` — selectors (exactly one): DIRECT `{scope_kind, scope_id,
  task?}`, or the REFERENCE lookup `{cmd_sha}` / `{profile}` (latest-by via a
  sidecar join).
- `mode: "diff"` — two trace keys (`left` / `right`) plus an optional
  caller-owned `tolerance` (`per_key` overrides fully replace the default;
  absent → exact).

A spec plus `--campaign-id`/`--run-id` is refused (`spec_invalid`) — one call,
one mode, never a silent preference.

## Outputs

One shape per mode (the union is `trace.output.json`):

- Lineage: `{trace_schema_version, scope, format, campaign_id, root,
  signature, node_count, nodes, edges, dot}` — `nodes` heterogeneous by
  `kind` (`campaign` / `run` / `wave`), `edges` directed by `rel`, `signature`
  the campaign's provenance-manifest self-attesting digest (campaign scope
  only), `dot` populated only in `dot` format.
- Render: the four-view markdown under `render` plus the structured rows
  (waterfall / label chains / feature lineage / sketch) and honest
  `present`/`skipped` absence fields.
- Diff: per-stage, per-atom divergences with the `first_divergence`
  `(stage, atom)` highlighted, structural (one-side-only stage) divergences
  named, and per-side `present` disclosure.

## Errors

- `spec_invalid` — (lineage) neither or both of `--campaign-id` / `--run-id`,
  or a `--run-id` with no journal record and no sidecar on disk; (any) a spec
  combined with the lineage scope args; (projections) a malformed spec.

An unknown *campaign* is not an error: it yields a well-formed DAG with just
the `campaign` root node (`run_count: 0`). A trace-store key the store never
held is disclosed (`present: false`), never fabricated.

## Idempotency

Idempotent by construction in every mode: all three are derived state,
recomputed from on-disk records on every call. No state is written.

## Notes

The per-run `provenance` fingerprint is projected through the same allowlist
`provenance-manifest` signs (`hpc_agent.ops.provenance_manifest.project_run_provenance`),
so the two surfaces never drift — `trace` is the navigable graph view, the
manifest is the flat signable artifact. The natural call points are mid- or
end-of-campaign (to see lineage and wave verdicts), post-mortem on a failed
run (`--run-id` to walk back through its resubmit ancestry), and
canary-vs-local / arm-vs-arm / today-vs-last-known-good comparisons
(`mode: "diff"`).

**Schemas:** [`trace.input.json`](../../src/hpc_agent/schemas/trace.input.json),
[`trace.output.json`](../../src/hpc_agent/schemas/trace.output.json).
