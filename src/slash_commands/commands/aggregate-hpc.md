`/aggregate-hpc` triggers the **aggregate** workflow — finalize a run's aggregated metrics.

This command is a thin trigger over `hpc-agent run`, the code-orchestrated entrypoint. Do not run the `hpc-aggregate` skill, and do not perform the workflow steps yourself in this conversation — the workflow runs in a fresh-context worker.

1. Structure the user's request into a JSON object `<fields>` — the `profile` (and optional `stage`) to aggregate, or `{}` to let the worker auto-discover which profiles/stages have results ready.
2. Run, via the `Bash` tool: `hpc-agent run aggregate --fields-json '<fields>'`. It spawns a fresh-context worker that executes the `hpc-aggregate` skill and prints a JSON envelope.
3. Surface to the user: `data.report.result` (`ok`, an aggregated-metrics summary, missing waves/tasks, escalation reason), `data.report.decisions`, and `data.report.anomalies`.
4. If a decision is an **escalation** — a partial-aggregation choice, an integrity violation to confirm — ask the user, add it to `<fields>`, and run `hpc-agent run aggregate` again.
