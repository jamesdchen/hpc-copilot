---
name: campaign-refill
verb: workflow
side_effects:
- scheduler-submit: <cluster> (per refilled slot)
- writes-campaign-state: <experiment_dir>/.hpc/runs/<run_id>.json (per refilled slot)
idempotent: true
idempotency_key: spec.campaign_id
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
- code: ssh_unreachable
  category: network
  retry_safe: true
- code: cluster_unknown
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent campaign-refill --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.campaign_refill.campaign_refill
---
# campaign-refill

The refill **actor** of a continuous-async campaign (RFC #362). Each tick it
asks [campaign-advance](campaign-advance.md) — the pure authority — whether a
greenlit async campaign has free pool slots with budget headroom, and if so
tops the pool back up by submitting `refill_count` fresh iterations, each as a
detached [campaign-run](campaign-run.md). It is the side-effecting arm that
consumes advance's `refill` decision; advance itself never submits.

## Why this exists

`campaign-advance` decides, per tick, whether the in-flight pool is below its
target `K` (`max_in_flight`) with jobs still in the budget — returning
`decision=="refill"` and a `refill_count`. Something has to act on that
decision by actually submitting the missing iterations. Before the
worker-removal wave that arm lived in a `deterministic_resolver`; it is now a
first-class primitive on the same block-drive spine as `campaign-run`, so the
whole refill loop is `campaign-watch → campaign-refill` chain steps with no
driver memory.

## Inputs

- `campaign_id` (string, required) — the greenlit async-refill campaign whose
  pool to top up this tick. That is the **complete** contract: `async_refill`,
  the pool target `K`, budget, and stop policy all default from the greenlit
  manifest via `campaign-advance`. Refill never re-specifies `K`, so the routing
  target ([campaign-watch](campaign-watch.md) / load-context) and the refill
  target read the same authority.

## Outputs

See `hpc_agent/schemas/campaign_refill.{input,output}.json`. The `data` block
carries a single `stage_reached` ∈ `{refilled, no_refill_needed,
refill_blocked}`, a `needs_decision` flag, the `decision` /`refill_count`
advance produced this tick, a `submitted` list (one row per spawned detached
`campaign-run` child: `run_id` + `detached_pid` + `stage_reached`), a `blocked`
list (slots that stopped at a resume-vs-fresh / scaffold escalation), and
`active_env_overrides` (the B15 transport-drift disclosure, mirroring
`campaign-run`).

| advance / slot outcome | `stage_reached` | `needs_decision` | next move |
| --- | --- | --- | --- |
| `decision != "refill"` (wait_in_flight / continue / stop_*) | `no_refill_needed` | false | typed no-op; the chain ends, next tick re-enters via campaign-watch |
| `decision == "refill"`, ≥1 slot spawned cleanly | `refilled` | false | detached children run; next tick re-enters via campaign-watch |
| a slot hit `prior_run_found` / `needs_scaffold_interview` mid-loop | `refill_blocked` | true | a human resolves resume-vs-fresh or runs the scaffold interview |

The `next_block` field is always `null` in practice — the chain **ends** at
`campaign-refill`; the next cron/loop tick re-enters through `campaign-watch`
(one-step-per-tick). The field is nonetheless declared so the MCP curated
catalog derives `campaign-refill` as a block.

## Errors

- `spec_invalid` — the campaign has no manifest, or the manifest is not
  greenlit (the standing-consent guard; greenlight is the one human boundary of
  an async campaign — the per-iteration refills carry none), or advance decided
  refill but the journal has no prior run to reconstruct the next iteration's
  submit context from.
- `ssh_unreachable` / `cluster_unknown` — surfaced from the per-slot submit
  path (`resolve-submit-inputs` → `campaign-run`) when the cluster is
  unreachable or unknown.

## Idempotency

Idempotent per tick (`idempotency_key = spec.campaign_id`), with **no new state
file and no cursor**. The whole refill decision is recomputed from journal state
via `campaign-advance` every tick; each submitted iteration writes its sidecar
immediately, so `campaign-status.in_flight` rises and the next tick's
`refill_count` shrinks. A partial tick (crash between a slot's sidecar write and
its detached spawn) therefore self-corrects — the orphan sidecar occupies a slot
until `load-context` / `doctor` reconciles it, but it already counts against the
pool so refill will not over-submit.

## Notes

- **Strictly sequential per slot (load-bearing).** The async optuna scaffold
  indexes its proposals by the campaign sidecar count and caches per index, so
  two slots that ask at the same count return the same trial. `campaign-refill`
  fully completes slot *i*'s `resolve-submit-inputs` — through the sidecar write
  that advances the count — before starting slot *i+1*. It never batch-builds
  all `K` specs and never parallelizes slots.
- **Refuses un-greenlit campaigns.** The greenlight is the standing consent for
  autonomous refill; iterations carry no per-iteration human boundary.
- Fast and sync-capable over `mcp-serve`: it resolves specs and spawns detached
  children, then returns — it never holds an SSH poll.
