---
name: block-drive
verb: workflow
side_effects:
- spawns-subprocess: hpc-agent <block verb> per chained span
- writes-journal: <run_id> pending_decision marker + watchdog tick
idempotent: true
idempotency_key: run_id
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
- code: journal_corrupt
  category: internal
  retry_safe: false
backed_by:
  cli: hpc-agent block-drive --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.block_drive_op.block_drive
---
## Purpose

The **wave-4 code-driven chain** (docs/design/block-drive.md §2–§5). One
stateless, resumable tick that DRIVES a block chain so the LLM no longer executes
the deterministic block→block transition. Generalizes the campaign reconcile-tick
driver (`_kernel/lifecycle/drive.py::drive_once`) to submit / status / aggregate /
campaign.

A single invocation:

1. chains the deterministic spans **in code** (S1-resolve → *decision*; or
   S2-canary → *decision*; …) via the re-homed successor table
   (`infra/block_chain.py`), consuming any already-journaled greenlight on the
   way (idempotent — re-reads the committed `y`, never re-asks);
2. at a human decision point, writes `{brief, pending-decision marker, resume
   cursor}` to durable state and **exits**. Nothing is held open between
   decisions — durable state (journal + filesystem) is the only thing carried,
   exactly like campaigns. This is deliberately **not** a parked/blocking
   process.

At a cluster-bound span (detached S2/S3/S4/speculate) the tick returns the detach
handle and exits — the detached child owns the poll.

## Inputs

A `BlockDriveSpec` JSON spec with:

- `run_id` (optional) — the run whose chain to advance. Absent on a fresh start.
- `workflow` (optional) — `submit` / `status` / `aggregate` / `campaign`;
  selects the first block on a fresh start (`block_chain.ORDER[workflow][0]`).
- `dry_run` (optional) — plan the next action and exit without executing it.

## Outputs

A `BlockDriveResult` with `{action, run_id, workflow, current_verb, next_verb,
stage_reached, brief, reason}`, where `action` ∈ `awaiting_decision` · `advanced`
· `reran` · `chained` · `detached` · `terminal` · `skip`.

- **`awaiting_decision`** — a block terminated at a decision point; the brief +
  pending marker + resume cursor are on disk and the tick exited. The human
  answers `y`/nudge in chat; on `y` the LLM commits the approved input spec to the
  decision journal's `resolved` and re-invokes `block-drive`.
- **`advanced` / `reran` / `advance_carrying`** — a resume consumed the committed
  `resolved` spec and routed by identity (`cmd_sha`) + field→stage ownership
  (`ops/field_ownership.py`, §4): unchanged → advance to the code-determined
  successor; changed field owned by the current block → re-run it; changed field
  owned downstream → advance carrying the edit.
- **`chained`** — a deterministic span with no decision point ran and the tick
  continued to the next span in code.
- **`detached`** — a scheduler-bound span spawned a detached child; the tick
  returned the watch handle.
- **`terminal`** — no deterministic successor and no decision; the chain is done.

The code **never reads a nudge string** — it reads `resolved` (an approved input
spec) and routes by identity + ownership. This is the "code never interprets raw
data / NL" invariant at the rendezvous (§3).

## Errors

- `spec_invalid` — malformed spec.
- `journal_corrupt` — no readable journal / decision record for the run.

## Idempotency

Idempotent on `run_id`. Re-running re-reads durable state: an un-consumed
greenlight is consumed once; an uncommitted decision point re-exits with the same
brief; a terminal chain is a no-op. Re-arming loses nothing — the same discipline
that makes the recovery machine (`doctor`, watchdog) safe. Each executed span
stamps the dead-man's-switch (`last_tick_at` / `next_tick_due`); the pending
marker flips the `doctor` read from "stalled — re-arm?" to "awaiting your decision
since T" so a parked driver never false-alarms (§5, parked ≠ stalled).

## Notes

Pairs with the decision-rendezvous Stop-hook: once the human's `y` is committed,
the hook blocks a turn-end until the next `block-drive` tick consumes `resolved`,
converting honor-system prose into harness-enforced continuation. Out of session,
the scheduled `doctor` tick advances the same committed-unconsumed `resolved`. The
`hpc-block-drive` console script is the invariant CLI substrate detach children
and OS schedulers invoke.
