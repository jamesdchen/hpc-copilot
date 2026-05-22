`/monitor-hpc` triggers the **status** workflow — poll an in-flight HPC run.

This command is a thin trigger over `hpc-agent run`, the code-orchestrated entrypoint. Do not run the `hpc-status` skill, and do not perform the workflow steps yourself in this conversation — the workflow runs in a fresh-context worker.

1. Structure the user's request into a JSON object `<fields>` — the `run_id` to check (if known) and whether they want a one-shot snapshot or to wait until the run is terminal. If `run_id` is unknown, pass `{}`; the worker resolves the in-flight run from on-disk state.
2. Run, via the `Bash` tool: `hpc-agent run status --fields-json '<fields>'`. It spawns a fresh-context worker that executes the `hpc-status` skill and prints a JSON envelope.
3. Surface to the user: `data.report.result` (`lifecycle_state`, complete/total, failed task ids, escalation reason), `data.report.decisions`, and `data.report.anomalies`.
4. If a decision is an **escalation** — the worker found several in-flight runs and needs the user to pick one, or a failed run needs a resubmit decision — ask the user, add it to `<fields>`, and run `hpc-agent run status` again.

For monitoring that must outlive the chat, schedule a recurring `hpc-campaign-driver --experiment-dir <dir>` (cron) or `/loop <interval> /monitor-hpc` — each tick is one fresh `hpc-agent run status`.
