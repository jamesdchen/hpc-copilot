---
name: campaign-watch
verb: workflow
side_effects: []
idempotent: true
idempotency_key: spec.campaign_id
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent campaign-watch --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.meta.campaign.blocks.campaign_watch
---
# campaign-watch

The **async execution surface** touchpoint of the campaign flow as a
human-amplification block (design §4). A read-only digest of a running campaign
for the anomaly / health briefs. After the spec is greenlit once, execution is
fully asynchronous — reconcile ticks self-chain while healthy and the strategy
chooses next batches deterministically — so there is **no per-iteration human
boundary**. `campaign-watch` only OBSERVES: it composes `campaign-advance`'s
folded evidence and classifies the campaign into one of three terminators. It
never runs a tick (ticks self-chain via the existing driver).

## Inputs

- `campaign_id` (str, required) — the running campaign to digest.

That is the whole spec: the greenlit manifest is the complete contract (§4), so
budget / stop / anomaly thresholds all default from it — watch reads them, it
never re-specifies them.

## Outputs

A `CampaignBlockResult`: `{block: "watch", stage_reached, needs_decision,
reason, campaign_id, brief}`. `stage_reached` is one of:

- `watching_healthy` (`needs_decision=false`) — nominal (`continue` /
  `wait_in_flight` / `refill`); execution self-chains, no boundary.
- `watching_anomaly` (`needs_decision=true`) — a §5 loud-fail guard tripped
  (`stop_circuit_breaker` / `stop_resubmit_cap`) or a budget halt
  (`stop_over_budget`); the drafted `anomaly_brief` is surfaced for the
  `y`/nudge decision.
- `watching_complete` (`needs_decision=false`) — a stop criterion fired
  (`stop_converged`); a hand-off hint to `campaign-complete`.

The `brief` carries the `campaign-advance` evidence verbatim (`decision`,
`status`, `budget`, `converged`, `circuit_breaker`, `resubmit_cap`,
`needs_acknowledgement`) plus the non-null `anomaly_brief` on a loud-fail
terminator. Never interpreted raw by the LLM.

## Errors

- `SpecInvalid` — an empty / malformed spec, or a filesystem-unsafe
  `campaign_id` (surfaced by the composed reads).

## Idempotency

Idempotent on `campaign_id`; a pure read that mutates nothing.

## Notes

The design intent is explicit: there is NO per-iteration human boundary. This
block observes and surfaces; it does not decide, does not tick, and does not
retry an anomaly. An anomaly is a block terminator handed back for `y`/nudge —
never silently retried by the LLM.
