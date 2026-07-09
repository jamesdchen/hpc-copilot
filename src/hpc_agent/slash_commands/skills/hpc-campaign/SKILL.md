---
name: hpc-campaign
description: "Start the campaign workflow with the code-driven chain (`block-drive`, first block `campaign-greenlight`) and relay each decision brief to the human for a `y`/nudge; on `y` commit the approved input spec to the journal's `resolved` and let the driver advance. A campaign spec is greenlit ONCE at start; execution then runs fully asynchronously (reconcile ticks self-chain in code) with no per-iteration human boundary — only anomaly briefs and the completion brief. The skill never resolves a decision and never interprets raw results."
allowed-tools: Bash Read Write
execution: inline
category: agent-autonomous
---

Start the campaign workflow by invoking the **`block-drive`** verb — the code-driven chain (design [§2](../../../../docs/design/human-amplification-blocks.md), [§4](../../../../docs/design/human-amplification-blocks.md), [§6](../../../../docs/design/block-drive.md)). It starts at the **`campaign-greenlight`** block and exits at each human touchpoint returning a **brief**. You are the translator at those rendezvous points **only**: render the brief as a proposal, take the human's `y` or nudge, and on `y` commit the approved input spec so the next `block-drive` tick advances. You do **not** read `next_block` and dispatch the next verb yourself — that sequencing is re-homed off the LLM into the driver's chaining table (design §6).

A campaign is **not** a linear per-run chain. Its spec — goal, budget, strategy, stop criteria, anomaly policy, async-refill — is **greenlit once at start** and is the complete contract; execution then runs **fully asynchronously against the spec** (reconcile ticks self-chain in code while healthy; the strategy picks batches deterministically) with **no per-iteration human boundary** (design §4). So there are exactly three campaign blocks (`meta/campaign/blocks.py`), one per §4 touchpoint: `campaign-greenlight` (start), `campaign-watch` (a read-only health/anomaly digest of the async execution — it observes, it never runs a tick), and `campaign-complete` (the completion brief). Each hands back `{block, stage_reached, needs_decision, reason, brief, next_block?, campaign_id?}`.

The slash `/campaign-hpc` is the human-interview wrapper (path picking, slug, spec authoring); an external autonomous agent invokes this skill directly.

## Invocation surface

- **Batch independent tool calls into one assistant message.** Multiple Bash / Read / Grep / Glob tool-call blocks in one message run concurrently. Do NOT use shell-level concurrency (`cmd1 & cmd2 & wait`, `parallel`, `xargs -P`) — trips the permission classifier as a compound command.
- **MCP-first (preferred):** the typed `campaign-greenlight` / `campaign-watch` / `campaign-complete` tools from `hpc-agent mcp-serve`.
- **Read-only QUERY verbs go DIRECT through MCP — never a spec-file round-trip.** `status-snapshot`, `read-decisions`, `verify-relay`, `doctor`, `net-triage` are pure reads: call the typed MCP tool with inline args and read the result — do NOT `Write` a `.hpc/specs/*.json` file and shell `--spec` just to read state back (three tool calls where one MCP call suffices). Never relay a number you remember; relay what the query returned.
- **CLI fallback:** one call per block, spec written to a file with the `Write` tool:
  ```bash
  hpc-agent campaign-greenlight --spec <path> --experiment-dir <dir>
  ```
  `--spec` takes a **file path only** — inline JSON (`--spec '{...}'`) is refused at the seam. Literally: `Write` the spec JSON to `.hpc/specs/campaign-greenlight.json`, then run
  ```bash
  hpc-agent campaign-greenlight --spec .hpc/specs/campaign-greenlight.json --experiment-dir .
  ```
  Parse the envelope from stdout. Read files with `Read`/`Grep`/`Glob`, never a shell `python -c` / `bash -c` / `jq` (the auto-mode classifier hard-blocks those). To get a verb's input schema, use `hpc-agent describe <verb> --schema` (or the MCP tool's `inputSchema`) — never `find`/`cat`/`inspect` a schema file.

## The driver loop

`block-drive` chains the three campaign touchpoints in code (`campaign-greenlight` → the async `campaign-watch` surface → `campaign-complete`); you translate at the rendezvous points it stops at. Each tick:

1. **Invoke `block-drive`.** The first call starts at `campaign-greenlight` — an un-greenlit manifest returns `needs_greenlight` with the digested spec brief. Later calls consume the approved spec from the journal's `resolved` and advance — or re-run `campaign-greenlight` for a fresh digest when a nudge edited the spec. The route is computed in code, never a verb you pick.
2. **Render the brief the driver returns as a proposal.** At greenlight, the digested spec; at an anomaly, the `anomaly_brief` (a §5 loud-fail guard tripped, or a budget halt); at completion, spend vs budget, iterations, stop reason, a code-extracted per-iteration outcome table, and an empty `proposed_interpretations` slot. Relay the code-drafted digest; never re-interpret it.
3. **The human answers `y` or nudges.** A single `y` approves the proposed input spec; a nudge edits the campaign spec (goal, budget, strategy, stop criteria) and re-presents. Loop until `y`.
4. **On `y`, commit the approved input spec to the journal's `resolved`, then invoke `block-drive` again to advance.** The commit *is* the approval (design §3, §5). Append the record:
   ```bash
   hpc-agent append-decision --spec <path> --experiment-dir <dir>
   ```
   `scope_kind: "campaign"`, `scope_id: <campaign_id>`, `block: <terminated block>`, `evidence_digest: <brief>`, `proposal: <what you surfaced>`, `response: "y"`, and the approved input spec under `resolved` (a spec, never the nudge string). At greenlight, the `confirm: true` path stamps the marker **and journals its own decision** (the block composes `append-decision`). **Do not end your turn after committing without firing the next tick** — the decision-rendezvous Stop-hook (design §5) blocks the stop until the driver advances. **Your final action MUST be a tool call, not a chat message** — the harness fires end-of-turn on any non-tool-call message, so a closing narration silently ends your turn and the driver never resumes; make the next `block-drive` tick the turn's last act.

A campaign is greenlit **once**, then runs asynchronously: `watching_healthy` (`continue` / `wait_in_flight` / `refill`) is **no boundary** — ticks self-chain in code; surface the health digest and let the human walk away. Only `watching_anomaly` and `watching_complete` are rendezvous points. **NEVER hand-compute a decision or interpret raw results:** code digests the campaign's durable state into each brief; the human decides.

On any connection failure (an SSH timeout, `ssh_unreachable`, `ssh_circuit_open`), run `hpc-agent net-triage` — the bounded, breaker-aware connectivity differential — before concluding a network cause; never diagnose with improvised ssh probes.

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
