---
name: campaign-complete
verb: workflow
side_effects: []
idempotent: true
idempotency_key: spec.campaign_id
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent campaign-complete --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.meta.campaign.blocks.campaign_complete
---
# campaign-complete

The **end** touchpoint of the campaign flow as a human-amplification block
(design §4). It builds the completion brief — a code digest over the campaign's
own durable state — for the final `y`/nudge propose loop: spend vs budget,
iteration count, the terminal stop reason, and a code-extracted per-iteration
outcome table, plus an EMPTY `proposed_interpretations` slot the LLM fills at
the boundary. Code extracts the outcomes; the human concludes from them (§2,
the #355 doctrine extended from computing results to concluding from them).

## Inputs

- `campaign_id` (str, required) — the campaign to build the completion brief
  for.

That is the whole spec: the brief is a digest of the campaign's manifest,
sidecars, and runtime-prior spend join — nothing else is needed.

## Outputs

A `CampaignBlockResult`: `{block: "complete", stage_reached: "complete",
needs_decision: true, reason, campaign_id, brief}`. Always a decision
terminator — the human interprets the outcomes. The `brief` carries:

- `goal` — the campaign goal from the manifest.
- `iterations` / `run_ids` — the completed-iteration count and ids.
- `spend` / `budget` / `remaining` / `coverage` — the `campaign-budget`
  roll-up (real compute joined from the runtime-prior store; `coverage`
  reports honest partial accounting).
- `stop_reason` — `{decision, reason}` from `campaign-advance` (the terminal
  decision, e.g. `stop_converged` / `stop_over_budget` / `stop_circuit_breaker`).
- `converged` / `anomaly_brief` — the convergence payload and, on a loud-fail
  terminal, the drafted anomaly brief.
- `outcome_table` — a stable per-iteration `{iteration, run_id, metrics}`
  projection of the code-extracted reduced metrics.
- `proposed_interpretations` — an EMPTY list. Code never fills it; concluding
  is the human's decision (§2).

## Errors

- `SpecInvalid` — an empty / malformed spec, or a filesystem-unsafe
  `campaign_id` (surfaced by the composed reads).

## Idempotency

Idempotent on `campaign_id`; a pure read that mutates nothing.

## Notes

Composes `campaign-status`, `campaign-budget`, and `campaign-advance` (all pure
reads) and the `compute_spend` runtime-prior join beneath budget. The empty
`proposed_interpretations` slot is the load-bearing invariant: the completion
brief hands the human code-extracted outcomes and a place for the LLM's drafted
interpretation options, but the interpretation itself is never encoded as tool
machinery — it enters only through the propose loop at decision time.
