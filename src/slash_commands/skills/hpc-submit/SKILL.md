---
name: hpc-submit
description: "Start the submit workflow with the code-driven chain (`block-drive`, first block `submit-s1`) and relay each decision brief to the human for a `y`/nudge; on `y` commit the approved input spec to the journal's `resolved` and let the driver advance. The blocks ARE the execution (code does SSH, staging, canary, submit, watch, harvest) and code drives the sequencing; this skill never resolves a decision point and never interprets raw results."
allowed-tools: Bash Read Write
execution: inline
category: agent-autonomous
---

Start the submit workflow by invoking the **`block-drive`** verb — the code-driven chain (design [§2](../../../../docs/design/human-amplification-blocks.md), [§6](../../../../docs/design/block-drive.md)). It starts at the **`submit-s1`** block, chains the deterministic spans in code, and exits at each human decision point returning a **brief**. You are the translator at those rendezvous points **only**: render the brief as a proposal, take the human's `y` or nudge, and on `y` commit the approved input spec so the next `block-drive` tick advances. You do **not** read `next_block` and dispatch the next verb yourself — that sequencing is re-homed off the LLM into the driver's chaining table (design §6).

The four submit blocks (`ops/submit_blocks.py`) `block-drive` composes are `submit-s1` (resolve) → `submit-s2` (stage & canary) → `submit-s3` (submit & watch) → `submit-s4` (harvest). Each is a code primitive that chains deterministically as far as code can, terminates at the first human decision point, and hands back `{block, stage_reached, needs_decision, reason, brief, next_block?, run_id?}`. **The blocks are the whole execution — there is no LLM inside them and no worker to hand off to.** The driver renders the brief; you take the human's answer and record it; the driver fires the next block.

The slash `/submit-hpc` is the human-interview wrapper; an external autonomous agent (MARs experiment-runner, notebook driver) invokes this skill directly. Either way the loop is the same — the difference is only who types the `y`/nudge.

## Invocation surface

- **Batch independent tool calls into one assistant message.** Multiple Bash / Read / Grep / Glob tool-call blocks in one message run concurrently. Do NOT use shell-level concurrency (`cmd1 & cmd2 & wait`, `parallel`, `xargs -P`) — trips the permission classifier as a compound command.
- **MCP-first (preferred).** When the harness has the registry-projected MCP server (`hpc-agent mcp-serve`), invoke each block as its typed tool (`submit-s1`, `submit-s2`, …) straight from the wire schema — no shell affordance, and cancel / raw-submit are structurally unreachable.
- **Read-only QUERY verbs go DIRECT through MCP — never a spec-file round-trip.** `status-snapshot`, `read-decisions`, `verify-relay`, `doctor`, and `net-triage` are pure reads: call the MCP typed tool with its args inline and read the result. Do NOT `Write` a `.hpc/specs/*.json` file and shell `hpc-agent <verb> --spec …` just to read state back — that is three tool calls (Write + Bash + Read) where one MCP call suffices, and proving run #5 burned ~6 of them correcting one relay this way. The Write-spec pattern below is for the *mutating* block verbs, and only when MCP is genuinely absent. **Never reconstruct state you can query:** relay the brief the block returned and the numbers `status-snapshot`/`verify-relay` report — never a figure you remember (a remembered count is the stale-relay bug the rule-10 Stop hook exists to catch).
- **CLI fallback** (harnesses without MCP): one call per block, spec written to a file:
  ```bash
  hpc-agent submit-s1 --spec <path> --experiment-dir <dir>
  ```
  Write the spec JSON with the `Write` tool and pass `--spec <path>` (never inline a shell-hostile JSON string). `--spec` takes a **file path only** — inline JSON (`--spec '{...}'`) is refused at the seam. Literally: `Write` the spec JSON to `.hpc/specs/submit-s1.json`, then run
  ```bash
  hpc-agent submit-s1 --spec .hpc/specs/submit-s1.json --experiment-dir .
  ``` Parse the block envelope from stdout. Read files with `Read`/`Grep`/`Glob`, never a shell `python -c` / `bash -c` / `jq` / `cat` (the auto-mode classifier hard-blocks those). To get a verb's input schema, use `hpc-agent describe <verb> --schema` (or the MCP tool's `inputSchema`) — never `find`/`cat`/`inspect` a schema file.

## The driver loop

`block-drive` drives the sequence in code; you translate at the rendezvous points it stops at. Each tick:

1. **Invoke `block-drive`.** The first call starts the chain at `submit-s1`; each later call consumes the approved spec from the journal's `resolved` and advances — or re-runs the block a nudge changed. The route is a **function of the spec** (design §4: identity + field→stage ownership), computed in code — never a verb you pick.
2. **Render the brief the driver returns as a proposal.** Relay the `reason` + `brief` (the code-digested evidence — resolved fields with pre-filled recommendations at S1, "canary green, est. N core-hours" at S2, the terminal status digest at S3, the code-extracted results table at S4). Never re-compute or re-interpret the brief's numbers — relay what code drafted.
   **At the S1 brief, fire `submit-speculate` BEFORE presenting it** (default, not opt-in — design §3's budget-1 speculative canary): the canary's queue+run time then overlaps the human's review, and a plain `y` finds S2 already done. This is doctrine-safe by construction — the canary is cheap, sandboxed, and idempotent; a spec-changing nudge moves the `cmd_sha` so the stale canary drains ignored (nudges never cancel it; no kill path); the TTL cache enforces the one-per-brief budget. Skip only when the S1 brief carries unresolved REQUIRED ambiguities (no recommended spec to speculate under).
3. **The human answers `y` or nudges.** A single `y` approves the proposed input spec; anything else is a natural-language nudge ("no, halve the grid and re-canary"), which you fold into the block's **inputs** (never a hand-edited derived *output* — that is the fabricated-field bug class) and re-present. Loop until `y`.
4. **On `y`, commit the approved input spec to the journal's `resolved`, then invoke `block-drive` again to advance.** The commit *is* the approval (design §3, §5). Write the decision record and append it:
   ```bash
   hpc-agent append-decision --spec <path> --experiment-dir <dir>
   ```
   `scope_kind: "run"`, `scope_id: <run_id>` (at the pre-resolve S1 boundary — `needs_resolution`, before a run_id is minted — use the literal `pre-run`; NEVER improvise another placeholder), `block: <the block that terminated>`, `evidence_digest: <brief>`, `proposal: <what you surfaced>`, `response: "y"`, and the approved input spec under `resolved` — the block-gate (`ops/block_gate.py`, `assert_greenlit_target`) and the driver read exactly this (a spec, never the nudge string), so the record is load-bearing, not bookkeeping. **Do not end your turn after committing without firing the next tick** — the decision-rendezvous Stop-hook (design §5) blocks the stop until the driver advances.

**`goal` and `task_generator` are HUMAN-AUTHORED — ask, never propose.** The run's goal and the sweep recipe (seed counts, sample sizes, axes) come only from the human: when the S1 brief surfaces either as a required field, ASK and wait for the answer. NEVER derive, recommend, or pre-fill a value for them from reading the repo — a table row recommending a sweep the human never stated is a conduct violation (proving run #4 fabricated "20 seeds" exactly this way), the same fabricated-field bug class as hand-editing a derived output.

**If `append-decision` is refused by the human-authorship gate, the remedy is the HUMAN, not a verb.** The gate verifies caller values against the utterance log, which only the harness writes — there is NO CLI verb that appends to it, so do not go hunting for one (proving run #5 burned a session doing exactly that). Relay the error's remedy verbatim: the human states the missing values themselves — a typed chat message, or text TYPED into a question tool's free-text field; a CLICK on an option you pre-filled carries no authorship and cannot commit them. Counts and ranges are fine ("20 seeds", "0 through 19") — the gate derives a consecutive sweep from its endpoints.

**A spec-changing nudge supersedes the prior attempt — close it out FIRST.** When a nudge changes cluster or resources after staging or submit (S2/S3), the earlier attempt does not vanish on its own: kill or reconcile it, or name it via the supersedes mechanism in the new spec, BEFORE re-submitting. Minting a fresh `run_id` cleans up nothing — proving run #4 orphaned three attempts this way, leaving staged trees and queued jobs live under abandoned run_ids.

Anomaly terminators (`stage_reached` = `canary_failed` / `watching_anomaly`) are genuine human branches (resubmit-failed / reconcile / kill) with no single deterministic successor — the driver surfaces the anomaly brief and the human's nudge names the recovery action. **NEVER hand-compute a decision or interpret raw results:** code (the blocks the driver composes) digests the evidence into the brief; the human decides; you only translate at the rendezvous. This extends the #355 doctrine ("results are never computed by an LLM") from computing to *concluding*.

On any connection failure (an SSH timeout, `ssh_unreachable`, `ssh_circuit_open`), run `hpc-agent net-triage` — the bounded, breaker-aware connectivity differential — before concluding a network cause; never diagnose with improvised ssh probes.

## Never-stall contract (blocks never block the chat)

Slow blocks are **detached by contract** (design §3, §7): `submit-s2` (canary wait) and `submit-s3` (main-array watch) return a handle immediately after spawning a durable detached watcher — you do **not** sit blocked on the scheduler. Keep working; the brief arrives as a notification. In the CLI fallback, run the block through your harness's native backgrounding (Claude Code's `run_in_background`), **never** a shell `&`. Detach survives session death; a successor session (or the doctor scan) re-arms from the journal losslessly.

**Await the worker — never poll on a timer.** Immediately after a block detaches, launch the waiter through the harness's backgrounding (Claude Code `run_in_background: true`):

```bash
hpc-agent wait-detached --spec <path with {"run_id": "<run_id>", "block": "<verb>"}>
```

`wait-detached` takes ONLY `--spec` — no `--experiment-dir` (the lease store is journal-global, keyed by run_id; proving run #5 hit an argparse exit 2 adding the flag out of habit). It blocks locally on the worker's lease pid (no SSH) and exits the moment the worker does — the harness then wakes you exactly once, with the brief ready to read from the journal. Do NOT schedule timed `/loop` wakeups to "check on" a detached worker (guessed cadences add dead air after the brief is ready and burn context re-reads); do NOT infer progress from the log or elapsed time while waiting (the reconcile rule below). A `timeout` outcome is not an anomaly — long queue waits are normal; re-arm another wait.

**While the waiter runs, do the parallel prep (every time, not optionally):** (1) pre-draft the next greenlight's `append-decision` spec from the already-approved `resolved` (only the brief's evidence_digest stays blank); (2) pre-write the next block's spec skeleton; (3) back-half preflight, read-only: `doctor` scan, `read-decisions` chain-coherence check, and the §5 watchdog probe — probe by the EXACT task name `hpc-agent-doctor-<repo_hash>` (`hpc-agent doctor-install` reports it; a bare name-prefix query false-negatives); (4) append to the run's report timeline, sourced only from the journal/briefs/sidecars. Never pre-run anything cluster-facing — the main array stays behind the human gate.

While a run is live, spawn a background tail of the local supervisor's output so the human sees liveness without asking (design §5 session tail-loop); if the session dies, output is recovered from the cluster by the guaranteed harvest on re-arm.

**Reconcile is the only source of run state** (`proving-run-2-hardening.md` Move 4). The tail is liveness *display*, never state: NEVER infer "still running" from an open log, a live pid, elapsed time, or an empty output file — proving run #2's driver reported a canary as "running, no result yet" from exactly those signals while the journal already recorded it failed. Run state comes ONLY from what the blocks read from the journal/reconcile (`status-snapshot`, the returned brief, `read-decisions`): report the state those return, and when the tail looks stale, invoke `status-snapshot` instead of narrating a guess.

## Speculative canary (DEFAULT at the S1 relay)

Fire `submit-speculate` when presenting the S1 brief (step 2 above — the default, skipped only when required ambiguities leave no recommended spec): it runs S2's canary early under the recommended defaults, so a plain `y` finds S2 already done and the canary's queue+run time hides inside the human's review. Nudges **never** cancel a speculative canary (design §3): a spec-changing nudge moves the `cmd_sha`, the stale canary drains and is ignored, and the next canary is fresh; an unchanged spec keeps the result. Budget is one speculative canary per pending brief, enforced by the canary TTL cache — no kill path.

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
