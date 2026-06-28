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
  cli: hpc-agent trace [--experiment-dir <dir>] [--campaign-id <campaign_id>] [--run-id
    <run_id>] [--format <trace_format>]
  python: hpc_agent.ops.trace.trace
---
# trace

Assemble a *derived* execution DAG for a campaign or a single run's lineage by
joining the three trace surfaces hpc-agent already records — the per-run
journal records (lifecycle state, wave verdicts, job ids), the per-run sidecars
(the immutable submit-time `{code, data, env, params}` fingerprint), and the
signable provenance manifest — into one replayable "explain exactly what
produced this result, and in what order" view. It is the read-side complement
to the OpenTelemetry sink: OTel streams the trace live to a backend like
Grafana; `trace` reconstructs it after the fact for replay, audit, or agent
consumption. Read-only and client-side — no SSH, no scheduler.

## Inputs

- `--campaign-id` (string) — trace every run tagged with this campaign_id.
  Mutually exclusive with `--run-id`.
- `--run-id` (string) — trace this run plus its transitive lineage (the
  `parent_run_ids` resubmit chain). Mutually exclusive with `--campaign-id`.
- `--format` (`dag` | `flat` | `dot`, default `dag`) — `dag` emits `run` and
  `wave` nodes plus `member` / `derived-from` / `contains` edges; `flat` emits
  the `run` nodes only, with no edges or wave nodes; `dot` emits the full
  `dag` plus a rendered Graphviz `dot` string (pipe to `dot -Tsvg`).
- `--experiment-dir` (path, default cwd) — the experiment root.

Exactly one of `--campaign-id` / `--run-id` is required.

## Outputs

`{trace_schema_version, scope, format, campaign_id, root, signature,
node_count, nodes, edges, dot}`.

- `scope` — `"campaign"` or `"run"`.
- `root` — the DAG root node id (`campaign:<id>` or `run:<seed>`).
- `signature` — the campaign's provenance-manifest self-attesting digest
  (64-hex); a reader can run `provenance-manifest` on the same campaign and
  confirm the signatures match. Campaign scope only — `null` for run scope.
- `nodes` — heterogeneous by `kind`: one `campaign` node, one `run` node per
  run (carrying `status`, `stage`, `cluster`, `profile`, `submitted_at`,
  `total_tasks`, `job_ids`, and a `provenance` fingerprint), and one `wave`
  node per wave with its `state` (`combined` / `failed` / `in_flight`) and
  `task_ids`.
- `edges` — directed, by `rel`: `member` (run→campaign), `derived-from`
  (run→parent run lineage), `contains` (run→wave). Empty in `flat` format.
- `dot` — a Graphviz DOT rendering of the DAG (node shape by kind, fill by
  lifecycle state, edge style by relation); populated only in `dot` format,
  `null` otherwise.

## Errors

- `spec_invalid` — neither or both of `--campaign-id` / `--run-id` supplied,
  or a `--run-id` with no journal record and no sidecar on disk.

An unknown *campaign* is not an error: it yields a well-formed DAG with just
the `campaign` root node (`run_count: 0`) — the absence of runs is itself a
fact worth recording, exactly as `provenance-manifest` treats it.

## Idempotency

Idempotent by construction. The DAG is derived state, recomputed from the
journal records and sidecars on every call, so replaying after more submits
simply reflects the runs now on disk. No state is written.

## Notes

The per-run `provenance` fingerprint is projected through the same allowlist
`provenance-manifest` signs (`hpc_agent.ops.provenance_manifest.project_run_provenance`),
so the two surfaces never drift — `trace` is the navigable graph view, the
manifest is the flat signable artifact. The natural call points are mid- or
end-of-campaign (to see lineage and wave verdicts) and post-mortem on a failed
run (`--run-id` to walk back through its resubmit ancestry).
</content>
