---
name: summarize-submit-plan
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent summarize-submit-plan --spec <path>
  python: hpc_agent.ops.submit.plan_summary.summarize_submit_plan
exit_codes:
- 0: ok
- 1: user-error
---

## Purpose

Render the canonical pre-submit confirmation summary for a `submit_flow.input.json` spec. Replaces the agent-rendered "here's what I'm about to launch" prose at `/submit-hpc` Step 5 with a deterministic, byte-stable summary the slash command prints verbatim.

Returns `{headline, body, confirm_prompt}`. `confirm_prompt` switches to a magnitude-warning shape when `total_tasks > 1000` so the agent surfaces the size up front.

## Compose with

- **Predecessors**: `build-submit-spec` (produces the spec this primitive summarizes).
- **Successors**: `submit-flow` (after the user confirms).

## Notes

- **Pure function** over the spec dict — no SSH, no filesystem reads, no schema re-validation (that's `build-submit-spec`'s job).
- **Byte-stable for the same input.** Two consecutive calls with the same spec produce byte-identical output, eliminating per-tick wording drift.
