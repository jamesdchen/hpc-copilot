---
name: hpc-campaign
description: "Start the campaign blocks (`campaign-greenlight`) and relay each block's code-digested brief to the human for a `y`/nudge, journaling every exchange and invoking exactly the block the envelope's `next_block` names. A campaign spec is greenlit ONCE at start; execution then runs fully asynchronously (reconcile ticks self-chain in code) with no per-iteration human boundary — only anomaly briefs and the completion brief. The skill never resolves a decision and never interprets raw results."
allowed-tools: Bash Read Write
execution: inline
category: agent-autonomous
---

Start the campaign workflow by invoking the **`campaign-greenlight`** block, then run the propose→`y`/nudge loop (design [§2](../../../../docs/design/human-amplification-blocks.md), [§4](../../../../docs/design/human-amplification-blocks.md)): surface each block's code-digested **brief** plus its machine-computed **`next_block`** suggestion to the human, collect a `y` or a natural-language nudge, journal the exchange, and on `y` invoke **exactly** `next_block.verb`.

A campaign is **not** a linear per-run chain. Its spec — goal, budget, strategy, stop criteria, anomaly policy, async-refill — is **greenlit once at start** and is the complete contract; execution then runs **fully asynchronously against the spec** (reconcile ticks self-chain in code while healthy; the strategy picks batches deterministically) with **no per-iteration human boundary** (design §4). So there are exactly three campaign blocks (`meta/campaign/blocks.py`), one per §4 touchpoint: `campaign-greenlight` (start), `campaign-watch` (a read-only health/anomaly digest of the async execution — it observes, it never runs a tick), and `campaign-complete` (the completion brief). Each hands back `{block, stage_reached, needs_decision, reason, brief, next_block?, campaign_id?}`.

The slash `/campaign-hpc` is the human-interview wrapper (path picking, slug, spec authoring); an external autonomous agent invokes this skill directly.

## Invocation surface

- **Batch independent tool calls into one assistant message.** Multiple Bash / Read / Grep / Glob tool-call blocks in one message run concurrently. Do NOT use shell-level concurrency (`cmd1 & cmd2 & wait`, `parallel`, `xargs -P`) — trips the permission classifier as a compound command.
- **MCP-first (preferred):** the typed `campaign-greenlight` / `campaign-watch` / `campaign-complete` tools from `hpc-agent mcp-serve`.
- **CLI fallback:** one call per block, spec written to a file with the `Write` tool:
  ```bash
  hpc-agent campaign-greenlight --spec <path> --experiment-dir <dir>
  ```
  Parse the envelope from stdout. Read files with `Read`/`Grep`/`Glob`, never a shell `python -c` / `bash -c` / `jq` (the auto-mode classifier hard-blocks those).

## The block loop

1. **Greenlight (once).** Invoke `campaign-greenlight`. An un-greenlit manifest returns `needs_greenlight` with the digested spec brief. Relay it; collect the human's `y` or nudge. On a nudge, the human edits the spec (or you re-draft) and re-invoke `campaign-greenlight` for a fresh digest. On `y`, re-invoke `campaign-greenlight` with `confirm: true` — that path stamps the greenlight marker **and journals the human's decision itself** (the block composes `append-decision`); it hands back `next_block: campaign-watch`.
2. **Watch (async surface).** Invoke `campaign-watch` — a pure read that digests the running campaign. `watching_healthy` (`continue` / `wait_in_flight` / `refill`) is **no boundary**: ticks self-chain, surface the health digest and let the human walk away. `watching_anomaly` (a §5 loud-fail guard tripped, or a budget halt) carries `needs_decision: true` and an `anomaly_brief` — surface it for a `y`/nudge and journal the answer. `watching_complete` (a stop criterion fired) hands off with `next_block: campaign-complete`.
3. **Complete.** Invoke `campaign-complete` — the completion brief: spend vs budget, iterations, stop reason, a code-extracted per-iteration outcome table, and an empty `proposed_interpretations` slot. Relay it; the human chooses the interpretation. Journal the exchange.

**Journal each human touchpoint** the skill surfaces (the anomaly and completion briefs; the greenlight block journals its own `confirm`):
```bash
hpc-agent append-decision --spec <path> --experiment-dir <dir>
```
`scope_kind: "campaign"`, `scope_id: <campaign_id>`, `block: <terminated block>`, `evidence_digest: <brief>`, `proposal: <what you surfaced>`, `response: "y"` or the nudge text.

## Never-stall

Campaign execution is asynchronous by design — after the greenlight there is **no** per-iteration wait. `campaign-watch` is a cheap read; poll it on a schedule (`/loop <interval> /campaign-hpc`, or a cron-scheduled tick) rather than blocking. Anomaly and completion briefs arrive as notifications from the async driver.

## Strategy authoring (path B — before greenlight)

A closed-loop campaign's `.hpc/tasks.py` **is** the strategy. Scaffold it with `hpc-agent scaffold-strategy --name {optuna,pbt} --output-dir <experiment_dir>` — never hand-roll a controller, and never `Read` the framework's `optuna_strategy.py` / `pbt_strategy.py` from site-packages to learn the contract. The load-bearing invariants the template already wires (you customize only the search space):

- **ask/tell run ONLY on the orchestrator; compute nodes call ONLY `resolve(task_id)`.** The optimizer import is local to `_propose`; proposals are indexed by completed count (load-idempotent).
- **`trial_token` is the reconciliation key** — stripped from `cmd_sha` (never busts dedup) but exported as `$HPC_KW_TRIAL_TOKEN` and re-paired with results; opaque bytes the framework never interprets.
- **`_optuna_trial_number` (or equivalent unique marker) is mandatory on path B** — without it repeat params collide on `cmd_sha`, the second iteration dedupes, and the campaign silently collapses. `campaign-greenlight`'s validation surfaces `missing_stochastic_marker` as a hard gate.
- **Custom reduce is an `aggregate_cmd` on the sidecar, run cluster-side** (env-var I/O; pulls back one JSON).

See [campaign-lifecycle.md](../../../../docs/internals/campaign-lifecycle.md) and [campaign-seam.md](../../../../docs/design/campaign-seam.md).

## Inputs

| Field | Source |
|---|---|
| `experiment_dir` | Required |
| `campaign_id` | Required |
| `path` | Caller (`"A"` manual grid; `"B"` strategy-driven) |

## Notes

- **The skill never resolves a decision and never interprets raw results.** Code digests the campaign's durable state (manifest, sidecars, budget join, stop reason) into each brief; the human decides the greenlight, any anomaly, and the final interpretation.
- **Greenlit once, then asynchronous.** There is no per-iteration human loop by design; async-refill correctness (drain-before-stop, budget headroom) is the driver's job, not a decision the skill relays.
- **Every `y`/nudge is journaled** under the campaign scope (append-only, one record per exchange) — the greenlight decision, the anomaly acknowledgements, and the completion interpretation.
