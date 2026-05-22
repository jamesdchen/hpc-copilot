`/submit-hpc` triggers the **submit** workflow — submit a parameter-grid experiment to an HPC cluster.

This command is a thin trigger over `hpc-agent run`, the code-orchestrated entrypoint. Do not run the `hpc-submit` skill, and do not perform the workflow steps yourself in this conversation — the workflow runs in a fresh-context worker.

1. Structure the user's request into a JSON object `<fields>` — the run or notebook to submit, plus any explicit choices they stated (`cluster`, `--no-canary`, `campaign_id`). No up-front interview is needed; pass whatever the user gave.
2. Run, via the `Bash` tool: `hpc-agent run submit --fields-json '<fields>'`. It validates the fields, generates the canonical worker prompt by code, and spawns a fresh-context worker that executes the `hpc-submit` skill. It prints a JSON envelope.
3. Surface to the user: `data.report.result` (run id, job ids, grid dimensions, verified scheduler state), `data.report.decisions` (each decision point the worker reached and why), and `data.report.anomalies`.
4. If a decision is an **escalation** — the worker needs an input only a human can give (a cluster choice, an axis classification, an executor to scaffold, a confirmation) — ask the user for it, add it to `<fields>`, and run `hpc-agent run submit` again. A fresh, unscaffolded experiment may take two round-trips.
