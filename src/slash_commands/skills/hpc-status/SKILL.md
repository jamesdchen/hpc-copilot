---
name: hpc-status
description: "Start the status workflow with the code-driven chain (`block-drive`, first block `status-snapshot`) and relay each decision brief to the human for a `y`/nudge; on `y` commit the approved input spec to the journal's `resolved` and let the driver advance. Snapshot is a cheap journal-first digest of what is running where and what changed since the human last looked; a live run advances to `status-watch`, a detached blocking poll to terminal or anomaly. The skill never resolves a decision and never interprets raw results."
allowed-tools: Bash Read Write
execution: inline
category: agent-autonomous
---

Start the status workflow by invoking the **`block-drive`** verb — the code-driven chain (design [§2](../../../../docs/design/human-amplification-blocks.md), [§6](../../../../docs/design/block-drive.md)). It starts at the **`status-snapshot`** block, chains the deterministic spans in code, and exits at each human decision point returning a **brief**. You are the translator at those rendezvous points **only**: render the brief as a proposal, take the human's `y` or nudge, and on `y` commit the approved input spec so the next `block-drive` tick advances. You do **not** read `next_block` and dispatch the next verb yourself — that sequencing is re-homed off the LLM into the driver's chaining table (design §6).

The two status blocks (`ops/status_blocks.py`) `block-drive` composes are `status-snapshot` (one-shot, journal-first digest: what is running where + what changed since `last_seen_by_human_at`, plus §5 stalled-driver and failed/abandoned anomaly detection) and `status-watch` (a blocking poll to terminal/anomaly, composing `monitor-flow` — which owns the throttled SSH spine and the §5 guaranteed terminal harvest). Each hands back `{block, stage_reached, needs_decision, reason, brief, next_block?, run_id?}`. **The blocks are the whole execution** — the poll loop, lifecycle transitions, and harvest all live in code; you only relay the driver's briefs and record the human's answer.

The slash `/monitor-hpc` is the human-interview wrapper; an external autonomous agent invokes this skill directly.

## Invocation surface

- **Batch independent tool calls into one assistant message.** Multiple Bash / Read / Grep / Glob tool-call blocks in one message run concurrently. Do NOT use shell-level concurrency (`cmd1 & cmd2 & wait`, `parallel`, `xargs -P`) — trips the permission classifier as a compound command.
- **MCP-first (preferred):** the typed `status-snapshot` / `status-watch` tools from `hpc-agent mcp-serve`.
- **Read-only QUERY verbs go DIRECT through MCP — never a spec-file round-trip.** `status-snapshot`, `read-decisions`, `verify-relay`, `doctor`, `net-triage` are pure reads: call the typed MCP tool with inline args and read the result — do NOT `Write` a `.hpc/specs/*.json` file and shell `--spec` just to read state back (three tool calls where one MCP call suffices). Never relay a number you remember; relay what the query returned.
- **CLI fallback:** one call per block, spec written to a file with the `Write` tool:
  ```bash
  hpc-agent status-snapshot --spec <path> --experiment-dir <dir>
  ```
  `--spec` takes a **file path only** — inline JSON (`--spec '{...}'`) is refused at the seam. Literally: `Write` the spec JSON to `.hpc/specs/status-snapshot.json`, then run
  ```bash
  hpc-agent status-snapshot --spec .hpc/specs/status-snapshot.json --experiment-dir .
  ```
  Parse the envelope from stdout. Read files with `Read`/`Grep`/`Glob`, never a shell `python -c` / `bash -c` / `jq` (the auto-mode classifier hard-blocks those). To get a verb's input schema, use `hpc-agent describe <verb> --schema` (or the MCP tool's `inputSchema`) — never `find`/`cat`/`inspect` a schema file.

## The driver loop

`block-drive` drives the sequence in code; you translate at the rendezvous points it stops at. Each tick:

1. **Invoke `block-drive`.** The first call starts the chain at `status-snapshot`; each later call consumes the approved spec from the journal's `resolved` and advances — or re-runs the block a nudge changed. The route is a **function of the spec** (design §4), computed in code — never a verb you pick.
2. **Render the brief the driver returns as a proposal.** Relay `reason` + `brief`: the snapshot's `running_where` / `changed_since_seen` / `stalled_runs` / `anomalies` rows, or the watch's terminal digest / anomaly evidence (counts, failed-wave ledger, the reporter's classified error, and a structured `recommendation` — proposed next-action DATA, never LLM-authored prose). Relay the code-drafted digest; never re-interpret the raw status.
3. **The human answers `y` or nudges.** A single `y` approves the proposed input spec; anything else is a nudge, which you fold into the block's **inputs** (never a hand-edited derived output) and re-present. Loop until `y`.
4. **On `y`, commit the approved input spec to the journal's `resolved`, then invoke `block-drive` again to advance.** The commit *is* the approval (design §3, §5). Append the record:
   ```bash
   hpc-agent append-decision --spec <path> --experiment-dir <dir>
   ```
   `scope_kind: "run"`, `scope_id: <run_id>`, `block: <terminated block>`, `evidence_digest: <brief>`, `proposal: <what you surfaced>`, `response: "y"`, and the approved input spec under `resolved` (a spec, never the nudge string). **Do not end your turn after committing without firing the next tick** — the decision-rendezvous Stop-hook (design §5) blocks the stop until the driver advances.

A `status-snapshot` with nothing live is terminal (`needs_decision: false`) — nothing to watch; surface and stop. A failed/abandoned anomaly or a stalled driver carries `needs_decision: true`: recovery (classify-then-resubmit, or reconcile-then-confirm before resubmit) is a human branch — the driver surfaces the recommendation and the nudge names the action. A `status-watch` that reaches a clean `complete` hands off to harvest (`submit-s4` — the guaranteed harvest already ran inside `monitor-flow`'s terminal path); a `timeout` (budget elapsed, cluster jobs may run on) keeps watching. **NEVER hand-compute a decision or interpret raw results:** code digests the status into the brief and the recommendation DATA; the human decides; you only translate at the rendezvous.

On any connection failure (an SSH timeout, `ssh_unreachable`, `ssh_circuit_open`, or a brief's `open_ssh_circuits` line), run `hpc-agent net-triage` — the bounded, breaker-aware connectivity differential — before concluding a network cause; never diagnose with improvised ssh probes.

## Never-stall + session tail-loop

`status-watch` is **detached by contract** (design §3): it returns a handle immediately after spawning a durable detached watcher rather than blocking on the poll; the terminal/anomaly brief arrives as a notification. In the CLI fallback, run it through your harness's native backgrounding (Claude Code's `run_in_background`), **never** a shell `&`. Detach survives session death; the doctor scan re-arms an orphaned run from the journal losslessly.

**Await the worker — never poll on a timer.** After the watch detaches, launch `hpc-agent wait-detached --spec <path with {"run_id": ..., "block": "status-watch"}>` via the harness's backgrounding (`run_in_background: true`): it blocks locally on the worker's lease pid and exits when the worker does, so the harness wakes you exactly once with the brief ready. No timed `/loop` wakeups, no log-tail state inference. A `timeout` outcome is normal on long queues — re-arm another wait.

**While the waiter runs, do the parallel prep (every time, not optionally):** (1) pre-draft the next greenlight's `append-decision` spec from the already-approved `resolved` (only the brief's evidence_digest stays blank); (2) pre-write the next block's spec skeleton; (3) back-half preflight, read-only: `doctor` scan, `read-decisions` chain-coherence check, and the §5 watchdog probe — probe by the EXACT task name `hpc-agent-doctor-<repo_hash>` (`hpc-agent doctor-install` reports it; a bare name-prefix query false-negatives); (4) append to the run's report timeline, sourced only from the journal/briefs/sidecars. Never pre-run anything cluster-facing — the main array stays behind the human gate.

While a run is live, **spawn a background tail of the local supervisor's output** (design §5 session tail-loop) so the human sees liveness without polling. If the chat session dies, job output is recovered from the cluster afterward by the guaranteed harvest once re-armed.

**Reconcile is the only source of run state** (`proving-run-2-hardening.md` Move 4). The tail is liveness *display*, never state: NEVER infer "still running" from an open log, a live pid, elapsed time, or an empty output file — proving run #2's driver reported a canary as "running, no result yet" from exactly those signals while the journal already recorded it failed. Run state comes ONLY from what the blocks read from the journal/reconcile (`status-snapshot`, the returned brief, `read-decisions`): report the state those return, and when the tail looks stale, invoke `status-snapshot` instead of narrating a guess.

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
