`/aggregate-hpc` is the **human-interview wrapper** around the `hpc-aggregate` skill — the agent-autonomous decision layer that combines a terminal HPC run's per-task results into a final metrics envelope.

## Invocation

Invoke the `hpc-aggregate` skill via the Skill tool with the initial spec:

```
Skill("hpc-aggregate", {
  experiment_dir: ".",
  profile: <if user stated>,
  run_id: <if user stated>,
  allow_partial: <if user requested>
})
```

The skill auto-discovers profile/run/stage from on-disk state; only fields the user pinned go in the initial spec.

## On `needs_resolution` — walking ambiguities

### Dialog: `profile`

Multiple profiles with terminal runs. Show candidates from the envelope:

```
Multiple profiles have terminal runs:
  1. ml_ridge — 3 runs, latest <run_id> (complete, 100/100)
  2. dl_patchts — 1 run, <run_id> (terminal_with_failures, 22/24)
Which profile?
```

### Dialog: `allow_partial`

```
Run <id> has <N>/<M> waves complete (<count> still running or failed). Aggregate on partial data?
  [Y]  proceed; mark envelope partial: true
  [n]  refuse; wait for the remaining waves (default)
```

Default **n** — partial aggregation usually masks real cluster issues.

## On final envelope

Surface to the user:
- `data.report.result.aggregated_metrics`
- `data.report.result.partial` flag if applicable
- `data.report.result.ingested_runtime_samples`
- `data.report.decisions`
- `data.report.anomalies`

## On `spec_invalid` (not `needs_resolution`)

- `nothing_to_aggregate`: "Nothing to aggregate — no terminal runs."
- `integrity_violation`: surface the code + evidence. Do NOT auto-proceed — these need investigation.

## Notes

- **Refuse partial by default.** Aggregating on incomplete waves silently produces wrong final metrics.
- **Idempotent.** Re-aggregating produces byte-identical output.
