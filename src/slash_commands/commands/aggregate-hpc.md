`/aggregate-hpc` is the **human-interview wrapper** around the `hpc-aggregate` skill — the agent-autonomous decision layer that combines a terminal HPC run's per-task results into a final metrics envelope.

This slash's job is purely human-elicitation: walk any profile/stage disambiguation dialog with the user, then invoke the `hpc-aggregate` skill. The decision logic — partial-aggregation handling, integrity-violation surfacing — lives in the skill.

## Interview

### Profile + run id

If `$ARGUMENTS` includes them, use them. Otherwise, load context:

```bash
hpc-agent load-context --experiment-dir .
```

Examine `data.runs` (terminal runs grouped by profile):

| Situation | Dialog |
|---|---|
| Single profile with terminal runs | Use the latest terminal `run_id` for that profile; skip. |
| Multiple profiles | "Multiple profiles have terminal runs:<br>1. ml_ridge — 3 runs, latest `<run_id>` (complete, 100/100)<br>2. dl_patchts — 1 run, `<run_id>` (terminal_with_failures, 22/24)<br>Which profile?" |
| No terminal runs | "Nothing to aggregate — no terminal runs in this experiment." Stop. |

Within a profile, default to the latest terminal `run_id` unless the user pins one.

### Partial aggregation

If `verify-aggregation-complete` reports incomplete waves, ask:

```
Run <id> has 18/20 waves complete (2 waves still running or failed). Aggregate
on partial data?
  [Y]  proceed; mark envelope partial: true
  [n]  refuse; wait for the remaining waves
```

Default **n** — partial aggregation usually masks real cluster issues.

## Handoff

Invoke the `hpc-aggregate` skill via the Skill tool:

```
Skill("hpc-aggregate", {
  experiment_dir: ".",
  profile: "<resolved>",
  run_id: "<resolved>",
  stage: "<resolved or omitted>",
  allow_partial: <true|false>,
  mode: "interview"
})
```

The skill verifies aggregation readiness, runs the combiner + reducer pipeline, and returns the aggregated metrics envelope.

Surface to the user:
- `data.report.result.aggregated_metrics` — the final metrics dict
- `data.report.result.partial` flag if applicable
- `data.report.result.ingested_runtime_samples` — count of new samples added to runtime priors
- `data.report.decisions` — which profile/stage/run_id chosen
- `data.report.anomalies` — reducer warnings, NaN inputs

## On `spec_invalid` from the skill

| Error code | What to do |
|---|---|
| `ambiguous_profile` | Show candidates; user picks. |
| `incomplete_aggregation` | Surface `missing_waves` count; ask user whether to wait or proceed partial. |
| `integrity_violation` | Surface the violation code + evidence. Do NOT auto-proceed — these need investigation (missing sidecars usually mean a per-task metric never landed). |

## Notes

- **Refuse partial by default.** Aggregating on incomplete waves silently produces wrong final metrics. The dialog above defaults to "wait" for exactly this reason.
- **Idempotent.** Re-aggregating the same `(run_id, profile, stage)` produces byte-identical output.
