---
name: hpc-submit
description: "Start the submit block chain (`submit-s1`) and relay each block's code-digested brief to the human for a `y`/nudge, journaling every exchange and invoking exactly the block the envelope's `next_block` names — until a terminal block. The blocks ARE the execution (code does SSH, staging, canary, submit, watch, harvest); this skill never resolves a decision point and never interprets raw results."
allowed-tools: Bash Read Write
execution: inline
category: agent-autonomous
---

Start the submit workflow by invoking the **`submit-s1`** block, then run the propose→`y`/nudge loop (design [§2](../../../../docs/design/human-amplification-blocks.md)): surface each block's code-digested **brief** plus its machine-computed **`next_block`** suggestion to the human, collect a `y` or a natural-language nudge, journal the exchange, and on `y` invoke **exactly** `next_block.verb` — never a hardcoded sequence, never a verb the envelope did not name — until a terminal block.

The four submit blocks (`ops/submit_blocks.py`) are `submit-s1` (resolve) → `submit-s2` (stage & canary) → `submit-s3` (submit & watch) → `submit-s4` (harvest). Each is a code primitive that chains deterministically as far as code can, terminates at the first human decision point, and hands back `{block, stage_reached, needs_decision, reason, brief, next_block?, run_id?}`. **The blocks are the whole execution — there is no LLM inside them and no worker to hand off to.** This skill is the relay: it renders the brief, takes the human's answer, records it, and fires the next block.

The slash `/submit-hpc` is the human-interview wrapper; an external autonomous agent (MARs experiment-runner, notebook driver) invokes this skill directly. Either way the loop is the same — the difference is only who types the `y`/nudge.

## Invocation surface

- **Batch independent tool calls into one assistant message.** Multiple Bash / Read / Grep / Glob tool-call blocks in one message run concurrently. Do NOT use shell-level concurrency (`cmd1 & cmd2 & wait`, `parallel`, `xargs -P`) — trips the permission classifier as a compound command.
- **MCP-first (preferred).** When the harness has the registry-projected MCP server (`hpc-agent mcp-serve`), invoke each block as its typed tool (`submit-s1`, `submit-s2`, …) straight from the wire schema — no shell affordance, and cancel / raw-submit are structurally unreachable.
- **CLI fallback** (harnesses without MCP): one call per block, spec written to a file:
  ```bash
  hpc-agent submit-s1 --spec <path> --experiment-dir <dir>
  ```
  Write the spec JSON with the `Write` tool and pass `--spec <path>` (never inline a shell-hostile JSON string). Parse the block envelope from stdout. Read files with `Read`/`Grep`/`Glob`, never a shell `python -c` / `bash -c` / `jq` / `cat` (the auto-mode classifier hard-blocks those).

## The block loop

Repeat until a terminal block (`next_block` is null **and** `needs_decision` is false, or the human ends the run):

1. **Invoke the block.** First iteration: `submit-s1`. Every later iteration: the verb named by the *previous* block's `next_block.verb`, seeded from its `spec_hint`. Only the block the envelope named is ever invoked.
2. **Relay the brief.** Render the envelope's `reason` + `brief` (the code-digested evidence — resolved fields with pre-filled recommendations at S1, "canary green, est. N core-hours" at S2, the terminal status digest at S3, the code-extracted results table at S4) and the `next_block` suggestion (its `verb` + `why`), the way `/sync` is proposed at the end of a work chunk. Never re-compute or re-interpret the brief's numbers — relay what code drafted.
3. **Collect the answer.** A single `y` greenlights the suggested `next_block`; anything else is a nudge (natural language — "no, halve the grid and re-canary").
4. **Journal the exchange** (design §2 — the decision record, not the chat scroll, is the source of truth). Write the record spec and append it:
   ```bash
   hpc-agent append-decision --spec <path> --experiment-dir <dir>
   ```
   `scope_kind: "run"`, `scope_id: <run_id>`, `block: <the block that terminated>`, `evidence_digest: <brief>`, `proposal: <what you surfaced>`, `response: "y"` or the nudge text. On a greenlight, put the greenlit verb under `resolved.next_block` (`resolved: {"next_block": "<next_block.verb>"}`) — the block-gate (`ops/block_gate.py`, `assert_greenlit_target`) reads exactly this and refuses a mis-sequenced block loudly, so the record is load-bearing, not bookkeeping.
5. **Advance.** On `y`, invoke `next_block.verb`. On a nudge, fold the nudge into the current block's spec and re-invoke the **same** block — it re-drafts a fresh brief; loop back to step 2. Anomaly terminators (`stage_reached` = `canary_failed` / `watching_anomaly`) carry `next_block: null` because recovery is a genuine human branch (resubmit-failed / reconcile / kill) with no single deterministic successor — surface the anomaly brief and let the human's nudge name the recovery action.

The greenlight gate makes the sequence self-enforcing: `submit-s2`/`s3`/`s4` each refuse unless the latest journaled decision for the run is a `y` naming *that* verb. Prose therefore never hardcodes the chain (design §2: "a guard the LLM itself satisfies is not a guard") — you invoke what the envelope named and the human greenlit, and code checks it.

## Never-stall contract (blocks never block the chat)

Slow blocks are **detached by contract** (design §3, §7): `submit-s2` (canary wait) and `submit-s3` (main-array watch) return a handle immediately after spawning a durable detached watcher — you do **not** sit blocked on the scheduler. Keep working; the brief arrives as a notification and rides the in-session tail-loop (see below). In the CLI fallback, run the block through your harness's native backgrounding (Claude Code's `run_in_background`), **never** a shell `&`. Detach survives session death; a successor session (or the doctor scan) re-arms from the journal losslessly.

While a run is live, spawn a background tail of the local supervisor's output so the human sees liveness without asking (design §5 session tail-loop); if the session dies, output is recovered from the cluster by the guaranteed harvest on re-arm.

## Speculative canary (opt-in)

To overlap the S1 review with the canary, invoke `submit-speculate` during the S1 `y`/nudge round — it runs S2's canary early under the recommended defaults, so a plain `y` finds S2 already done. Nudges **never** cancel a speculative canary (design §3): a spec-changing nudge moves the `cmd_sha`, the stale canary drains and is ignored, and the next canary is fresh; an unchanged spec keeps the result. Budget is one speculative canary per pending brief, enforced by the canary TTL cache — no kill path.

## Inputs

| Field | Source |
|---|---|
| `experiment_dir` | Required (absolute path) |
| `cluster` | Caller, else surfaced as an S1 recommendation from `clusters.yaml` |
| `task_generator` | Caller (surfaced as a required S1 field when no `tasks.py` exists — it cannot be auto-invented; the human supplies it via nudge) |
| `no_canary` | Caller (default `false`) |
| `campaign_id` | Caller (pass-through) |

## Notes

- **The skill never resolves a decision and never interprets raw results.** Code (the blocks) digests evidence and drafts the brief; the human decides; you relay both directions. This extends the #355 doctrine ("results are never computed by an LLM") from computing to *concluding*: at S4 the code hands over an empty `proposed_interpretations` slot and a results table — the human chooses the interpretation.
- **`apply-safe-defaults` is dead as a silent actor.** Each S1 ambiguity's old safe-default survives only as a pre-filled `recommendation` inside the brief that the human greenlights or nudges — nothing is auto-applied into the resolved plan.
- **Every `y`/nudge is journaled**, including each nudge round (append-only, one record per exchange) — so the trail shows the sequence of nudges that shaped the run, not just the endpoint.
