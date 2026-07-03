---
name: hpc-status
description: "Start the status blocks (`status-snapshot`) and relay each block's code-digested brief to the human for a `y`/nudge, journaling every exchange and invoking exactly the block the envelope's `next_block` names. Snapshot is a cheap journal-first digest of what is running where and what changed since the human last looked; a live run's `next_block` is `status-watch`, a detached blocking poll to terminal or anomaly. The skill never resolves a decision and never interprets raw results."
allowed-tools: Bash Read Write
execution: inline
category: agent-autonomous
---

Start the status workflow by invoking the **`status-snapshot`** block, then run the propose→`y`/nudge loop (design [§2](../../../../docs/design/human-amplification-blocks.md)): surface each block's code-digested **brief** plus its machine-computed **`next_block`** suggestion to the human, collect a `y` or a natural-language nudge, journal the exchange, and on `y` invoke **exactly** `next_block.verb` — never a hardcoded sequence, never a verb the envelope did not name.

The two status blocks (`ops/status_blocks.py`) are `status-snapshot` (one-shot, journal-first digest: what is running where + what changed since `last_seen_by_human_at`, plus §5 stalled-driver and failed/abandoned anomaly detection) and `status-watch` (a blocking poll to terminal/anomaly, composing `monitor-flow` — which owns the throttled SSH spine and the §5 guaranteed terminal harvest). Each hands back `{block, stage_reached, needs_decision, reason, brief, next_block?, run_id?}`. **The blocks are the whole execution** — the poll loop, lifecycle transitions, and harvest all live in code; this skill only relays briefs and records the human's answer.

The slash `/monitor-hpc` is the human-interview wrapper; an external autonomous agent invokes this skill directly.

## Invocation surface

- **Batch independent tool calls into one assistant message.** Multiple Bash / Read / Grep / Glob tool-call blocks in one message run concurrently. Do NOT use shell-level concurrency (`cmd1 & cmd2 & wait`, `parallel`, `xargs -P`) — trips the permission classifier as a compound command.
- **MCP-first (preferred):** the typed `status-snapshot` / `status-watch` tools from `hpc-agent mcp-serve`.
- **CLI fallback:** one call per block, spec written to a file with the `Write` tool:
  ```bash
  hpc-agent status-snapshot --spec <path> --experiment-dir <dir>
  ```
  Parse the envelope from stdout. Read files with `Read`/`Grep`/`Glob`, never a shell `python -c` / `bash -c` / `jq` (the auto-mode classifier hard-blocks those).

## The block loop

Repeat until a terminal block (`next_block` null and `needs_decision` false, or the human ends the run):

1. **Invoke the block.** First iteration: `status-snapshot`. Later iterations: the verb the previous block's `next_block.verb` named, seeded from its `spec_hint`.
2. **Relay the brief.** Render `reason` + `brief`: the snapshot's `running_where` / `changed_since_seen` / `stalled_runs` / `anomalies` rows, or the watch's terminal digest / anomaly evidence (counts, failed-wave ledger, the reporter's classified error, and a structured `recommendation` — proposed next-action DATA, never LLM-authored prose). Relay the code-drafted digest; never re-interpret the raw status.
3. **Collect the answer.** A single `y` greenlights the suggested `next_block`; anything else is a nudge.
4. **Journal the exchange:**
   ```bash
   hpc-agent append-decision --spec <path> --experiment-dir <dir>
   ```
   `scope_kind: "run"`, `scope_id: <run_id>`, `block: <terminated block>`, `evidence_digest: <brief>`, `proposal: <what you surfaced>`, `response: "y"` or the nudge text; on a greenlight, `resolved: {"next_block": "<next_block.verb>"}`.
5. **Advance.** On `y`, invoke `next_block.verb`. On a nudge, fold it into the current block's spec and re-invoke the same block (it re-drafts a fresh brief). A `status-snapshot` with nothing live returns `next_block: null` and `needs_decision: false` — nothing to watch; surface and stop. A failed/abandoned anomaly or a stalled driver carries `next_block: null` and `needs_decision: true`: recovery (classify-then-resubmit, or reconcile-then-confirm before resubmit) is a human branch — surface the recommendation and let the nudge name the action.

A `status-watch` that reaches a clean `complete` returns `needs_decision: false` and a `next_block` of `submit-s4` (the guaranteed harvest already ran inside `monitor-flow`'s terminal path) — the hand-off to harvest. A `timeout` (budget elapsed, cluster jobs may run on) suggests `status-watch` again to keep watching.

## Never-stall + session tail-loop

`status-watch` is **detached by contract** (design §3): it returns a handle immediately after spawning a durable detached watcher rather than blocking on the poll; the terminal/anomaly brief arrives as a notification. In the CLI fallback, run it through your harness's native backgrounding (Claude Code's `run_in_background`), **never** a shell `&`. Detach survives session death; the doctor scan re-arms an orphaned run from the journal losslessly.

While a run is live, **spawn a background tail of the local supervisor's output** (design §5 session tail-loop) so the human sees liveness without polling. If the chat session dies, job output is recovered from the cluster afterward by the guaranteed harvest once re-armed.

## Inputs

| Field | Source |
|---|---|
| `experiment_dir` | Required |
| `run_id` | Caller, else the snapshot digests the whole in-flight fleet |
| `wait_terminal` | Caller — when the human asks to wait, greenlight straight to `status-watch` from the snapshot |

## Notes

- **The skill never resolves a decision and never interprets raw results.** Code digests the status into the brief and drafts the recommendation DATA; the human decides.
- **Auto-resubmit is never the default.** A failed run surfaces as an anomaly whose recommendation is classify-then-resubmit — the human greenlights it; silent auto-resubmit (re-running the same bug) is not a code path.
- **Every `y`/nudge is journaled** (append-only, one record per exchange).
