---
name: validate-stochastic-marker
verb: validate
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-agent validate-stochastic-marker --spec <path>
  python: claude_hpc.atoms.validate_stochastic_marker.validate_stochastic_marker
---
# validate-stochastic-marker

Pre-submit cross-iteration check for closed-loop campaigns: detect when the about-to-submit run's `cmd_sha` collides with a prior iteration of the same campaign — the silent-dedup bug class. Stochastic strategies (Optuna, random-search, PBT) re-pick the same params across iterations from time to time; without a unique-per-iteration discriminator field (idiomatic: `_optuna_trial_number`) inside `tasks.resolve()`, two iterations with identical params would compute the same `cmd_sha`, and the second one would dedupe at submit time, collapsing the campaign silently. This validator catches the collision at submit, not 6h into the campaign.

## Inputs

- `campaign_id` (string, required) — Closed-loop campaign slug. Must match `^[A-Za-z0-9._\-]+$`.
- `expected_cmd_sha` (string, required, ≥8 chars) — The cmd_sha the about-to-submit run will have, computed via `compute_cmd_sha(load_tasks_module(.hpc/tasks.py))` BEFORE invoking submit-flow.

## Outputs

A `ValidateStochasticMarkerResult` object with:

- `findings` (list of `ValidatorFinding`) — Empty list = pass. When a collision exists, a single error finding is emitted with `code="stochastic_marker_missing"`, message naming the prior run_id and total collision count, and a `suggested_fix` recommending a unique-per-iteration field.
- `matched_prior_run_ids` (list of run_id strings, newest-first) — Run IDs of prior iterations sharing this `cmd_sha`. Empty when no collision (the typical pass case); populated as evidence when a collision fires.

## Errors

None declared on the primitive. Spec validation errors raise `pydantic.ValidationError`. Findings carry the diagnostic code:

- `stochastic_marker_missing` (error) — at least one prior iteration of the same campaign already carries this `cmd_sha`. The submit would dedupe silently.

## Idempotency

Pure local read of sidecars under `<experiment_dir>/.hpc/runs/`; calling twice with the same inputs produces the same result.

## Notes

- **Path A (manual params) campaigns don't need this validator.** When the user enumerates a fixed grid in `tasks.py` themselves, the param tuple is unique per iteration by construction. The validator is for **Path B** (strategy-driven) campaigns where Optuna/random-search/PBT may re-sample the same params.
- **The marker should live in `tasks.resolve()`'s output.** Idiomatic name: `_optuna_trial_number` (an integer that increments per `study.ask()` call). Any unique field works — `_iteration_index`, `_seed`, `_replication_id` — as long as it differs across iterations.
- **The check fires only when the collision is real.** A campaign whose first iteration just happens to share params with a future iteration won't trip this validator at first-submit time; it fires on iteration N when iteration N's `cmd_sha` matches some prior. By that point the user can fix `tasks.py` and re-submit before the campaign's investment is wasted.
- **Composed by `validate-campaign`** when both `campaign_id` and `expected_cmd_sha` are set on the workflow's spec — the workflow skips this atom otherwise (consistent with the other independently-skippable validators).
- **Not a wire contract for the slash command's UX** — the bug class it catches is invisible until the framework's dedup engages. Prose in `hpc-campaign/SKILL.md` describes the marker requirement; this atom mechanically verifies compliance.
