---
name: validate
verb: validate
side_effects:
- ssh: <cluster> (scheduler --test-only probe)
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-mapreduce validate --profile <p> --cluster <c> --walltime-sec <s> --mem-mb
    <m> --cpus <c>
  python: claude_hpc.orchestrator.validate.validate_submission
---

## Purpose

**Validation-first** primitive (LARA-HPC pattern). Probe the scheduler\'s
`--test-only` mode for a resource ask; return the predicted start time
without submitting anything. MARs branch on the result — *fits the
30-minute backfill window?* *queue is hours deep, postpone?* — instead
of committing to a blind submit.

Distinct from [score-submit-plan](score-submit-plan.md): that primitive
sweeps the full `(constraint × walltime)` lattice and picks an optimum;
`validate` is a single point check the caller has already chosen. Use
`validate` when you have one resource ask and want to know "now or later".

## Inputs

See `schemas/validate.input.json`. At minimum: `profile`, `cluster`,
`walltime_sec`, `mem_mb`, `cpus`. Optional: `constraint` (GPU feature),
`gpus`, `backfill_window_sec` (threshold for the `fits_backfill` flag;
defaults to 600s).

## Outputs

See `schemas/validate.output.json`. The envelope's `data` block carries:

* `estimated_start_iso` — scheduler\'s predicted start (UTC ISO).
* `predicted_eta_sec` — seconds from now until start, or `null` on
  parse failure / non-SLURM scheduler.
* `fits_backfill` — `predicted_eta_sec <= backfill_window_sec`.
* `reason` — short human summary.
* `scheduler_response` — raw probe text, clamped to 2000 chars.

## Compose with

* Common predecessors: [check-preflight](check-preflight.md) (SSH up?),
  [score-submit-plan](score-submit-plan.md) (which constraint?).
* Common successors: [submit-flow](submit-flow.md) (submit when
  `fits_backfill=true`) or postpone / re-validate later.

## Exit codes

* 0 — probe completed (`ok=true`); see `data` for timing.
* 2 — scheduler unreachable / throttled.
