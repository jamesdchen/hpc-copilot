---
name: campaign-advance
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-agent campaign advance --campaign-id <id>
  python: claude_hpc.atoms.campaign_advance.campaign_advance
---
# campaign-advance

> **Internal primitive** — not surfaced in `capabilities --full`.
> The agent uses higher-level workflow atoms (the `/campaign-hpc`
> slash command) which compose this internally; direct invocation
> is for debugging and bespoke campaign loops.

Recommend the next action for a running campaign by composing
`campaign-status`, `campaign-converged`, and `campaign-budget`
into a single decision. Returns one of `continue`,
`stop_converged`, `stop_over_budget`, or `wait_in_flight` plus a
human-readable `reason`.

## Composers

- Bespoke campaign-loop scripts (user-side strategy code that
  drives `submit-flow → monitor-flow → aggregate-flow → advance`
  without going through the slash command).
- The `/campaign-hpc` slash command at the "decide whether to
  iterate again" step.

No registered Python `composes=` references — `campaign-advance`
is agent-driven from above, never composed by a workflow
primitive.

## Invariants

- **Pure read.** No journal mutation, no SSH; only reads campaign
  sidecars under `<experiment>/.hpc/runs/`.
- **Stateless across calls.** Stop criteria + budget caps come in
  as kwargs; the framework holds no opinion about defaults. The
  campaign manifest (`<campaign_dir>/manifest.json`) is the
  durable home for these values, but `campaign-advance` re-reads
  them on every invocation.
- **First-match-wins ordering.** When multiple decisions could
  fire (converged AND over budget), `stop_converged` wins over
  `stop_over_budget` over `wait_in_flight` over `continue`.

## Coupling

- Stop-criteria semantics live in `campaign-converged`; budget-cap
  semantics in `campaign-budget`. Adding a new criterion means
  adding it to the composing primitive AND threading the new
  kwarg through `campaign-advance`.
- The four `decision` enum values are the public contract. Adding
  one is a wire-breaking change; downstream agents key on the set.

## Failure modes

- Agent forgets to pass stop criteria → returns
  `decision="continue"` because no criterion fired (silent
  proceed). Mitigation: the calling loop should validate that AT
  LEAST one of `max_iters`, `target`, `plateau_window` is supplied.
- Campaign with zero completed iterations → returns
  `wait_in_flight` regardless of criteria; strategy code should
  not interpret this as "continue" (no new submit yet).
