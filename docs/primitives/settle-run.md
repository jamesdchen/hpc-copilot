---
name: settle-run
verb: workflow
side_effects:
- writes-journal: <experiment>/.hpc/decisions/run/<run_id>.jsonl (the directed-settle
    sign-off) + the run record's terminal status
- ssh: <cluster> (harvest_on_terminal summary pull; best-effort, on a transition)
idempotent: true
idempotency_key: run_id
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent settle-run --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.settle_run.settle_run
---
# settle-run

Human-directed terminal settle through the **same** machinery the probe path uses
(run-12 finding 25). Given directed terminal evidence, it journals the evidence as
a sign-off, sets the terminal status via `mark_run`, and runs the same
transition-gated `harvest_on_terminal` (summary pull + transition stamp) the
automatic reconcile arm runs — so a directed settle is byte-for-byte the same
lifecycle event as a probed one. This replaces the journal surgery that closed
run 12 (a hand-edit that bypassed harvest and carried prose instead of counts).

## Inputs

- `run_id` (str) — the run to settle.
- `status` (`complete` | `failed` | `abandoned`) — the terminal state the evidence
  proves. A non-terminal status is refused.
- `evidence` (str) — the directed evidence statement (required).
- `artifact_refs` (list[str], optional) — corroborating result-tree paths / logs.
- `task_counts` (dict[str,int], optional) — typed counts recorded in `last_status`
  (the counts the prose hand-edit lacked).
- `provenance` (str, optional) — how the evidence was captured (default
  `human-directed`).

## Outputs

`{stage_reached: "settled" | "already_terminal", run_id, status, prior_status,
harvested, harvest, decision_ts, reason}` — `harvested` is True (and `harvest`
carries the marker) exactly when the status transitioned.

## Errors

- `spec_invalid` — no such run; a non-terminal `status`; or empty `evidence` (a
  settle with no evidence is the surgical status-flip this verb replaces).

## Idempotency

Keyed on `run_id`. The harvest fires only on a status **transition** — an
idempotent re-settle of an already-terminal run records the sign-off but does not
re-pull (each harvest pays an rsync + reduce + a ledger append), mirroring the
reconcile settle arm's transition gate.

## Notes

The directed evidence is journaled as a `run`-scoped decision (block
`settle-run`, response `y`) with its provenance — the sign-off trail the hand-edit
lacked. `mark_run` + `harvest_on_terminal` are the same calls the reconcile arm
makes.
