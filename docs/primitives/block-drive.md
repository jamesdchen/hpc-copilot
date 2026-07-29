---
name: block-drive
verb: workflow
side_effects:
- spawns-subprocess: hpc-agent <block verb> per chained span
- writes-journal: <run_id> pending_decision marker + watchdog tick + drive_attempts
    counter
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
  owned downstream → advance carrying the edit. `advanced` is ALSO what a tick
  that LOST the consumption compare-and-swap reports — a concurrent driver
  consumed this boundary first, so this tick ran no span; the `reason` names the
  other driver and a fresh status read shows the winner's position (see
  Idempotency).
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
that makes the recovery machine (`doctor`, watchdog) safe.

**Consumption is a compare-and-swap, so "consumed once" holds under CONCURRENT
drivers too** (run-queue plan §8 S8). Two drivers tick the same parked run at
every `y` by design — the main session's inline first tick and the auto-launched
drain pass — and both read the same pending marker before either clears it. The
tick therefore consumes the marker through
`state/journal.compare_and_clear_pending_decision`: inside the journal's per-run
flock it verifies the marker on disk is still the `(block, awaiting_since)` pair
this tick read, and only then clears it. Exactly one driver wins and runs the
successor span. The loser runs no span, writes nothing (the swap sits ahead of
every write the resume leg makes — the span, its watchdog stamp, the next park),
never re-parks the consumed decision, and never raises: it returns `advanced`
with a `reason` naming the other driver, so a drain pass re-reads status instead
of being charged a futile tick. The F14 failed-span re-park is the same swap in
reverse (`compare_and_repark_pending_decision`, which only lands in an empty
slot), so a stale marker can never resurrect a boundary another driver already
consumed and moved past. Each executed span
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

This agent-facing seat is also the ONE write point for the run queue's
retryable(n) budget (run-queue plan §7). After a non-`dry_run` tick it stamps
`RunRecord.drive_attempts`: `+1` when the tick moved nothing
(`awaiting_decision` / `skip`), reset to `0` on any action that advanced,
reran, chained, detached or reached a terminal. `queue-status` projects it per
item, so a drain plan can bound how many futile ticks it relays at one item and
still lose nothing across a relaunch — the budget lives in the kernel, not in a
plan variable. The console-script / detach-child entry reaches `run_tick`
directly and is deliberately NOT counted: a detached child's poll is not a pass
relaying a tick, and counting it would let a healthy long wait exhaust an item's
budget. Bookkeeping never raises — a drive that happened must not be reported as
an error because its counter could not be written.
