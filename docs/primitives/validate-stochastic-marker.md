---
name: validate-stochastic-marker
verb: validate
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: (none — Python-only primitive)
  python: hpc_agent.ops.validate.stochastic_marker.validate_stochastic_marker
---
# validate-stochastic-marker

> **Internal primitive** — not surfaced in `capabilities --full`.
> No CLI subcommand and no MCP tool exists to invoke it, so no agent can
> call it directly; `validate-campaign` composes it and its findings reach
> the agent folded into that envelope. `validate-campaign` is the
> agent-facing verb.

Pre-submit cross-iteration check for closed-loop campaigns: detect when the about-to-submit run's `cmd_sha` collides with a prior iteration of the same campaign — the silent-dedup bug class. Stochastic strategies (Optuna, random-search, PBT) re-pick the same params across iterations from time to time; without a unique-per-iteration discriminator field (idiomatic: `_optuna_trial_number`) inside `tasks.resolve()`, two iterations with identical params would compute the same `cmd_sha`, and the second one would dedupe at submit time, collapsing the campaign silently. This validator catches the collision at submit, not 6h into the campaign.

## Composers

- **`validate-campaign`** — composes it when both `campaign_id` and
  `expected_cmd_sha` are set on the workflow's spec, and skips the atom
  otherwise (consistent with the other independently-skippable validators).

The campaign worker prompt
(`src/hpc_agent/_kernel/extension/worker_prompts/campaign.md`) describes the
marker requirement in prose; this atom is what mechanically verifies
compliance. It is not a wire contract for the slash command's UX — the bug
class is invisible until the framework's dedup engages.

## Contract surface

Takes `campaign_id` (closed-loop campaign slug, must match
`^[A-Za-z0-9._\-]+$`) and `expected_cmd_sha` (≥8 chars — the cmd_sha the
about-to-submit run will have, computed via
`compute_cmd_sha(load_tasks_module(.hpc/tasks.py))` BEFORE invoking
`submit-flow`).

Returns a `ValidateStochasticMarkerResult` with:

- `findings` — empty on pass. On collision, a single error finding with
  `code="stochastic_marker_missing"`, a message naming the prior run_id and
  the total collision count, and a `suggested_fix` recommending a
  unique-per-iteration field.
- `matched_prior_run_ids` — prior iterations sharing this `cmd_sha`,
  newest-first. Empty on the typical pass; populated as evidence on collision.

## Invariants

- **Path B only.** Path A (manual params) campaigns do not need this gate:
  when the user enumerates a fixed grid in `tasks.py`, the param tuple is
  unique per iteration by construction. This is for strategy-driven campaigns
  where Optuna / random-search / PBT may re-sample the same params.
- **Fires only on a real collision.** A campaign whose first iteration happens
  to share params with a future iteration does not trip at first-submit; it
  fires on iteration N when N's `cmd_sha` matches some prior. The user can fix
  `tasks.py` and re-submit before the campaign's investment is wasted.
- **The marker belongs in `tasks.resolve()`'s output.** Idiomatic name
  `_optuna_trial_number` (incrementing per `study.ask()`), but any field that
  differs across iterations works — `_iteration_index`, `_seed`,
  `_replication_id`.
- **Spec errors raise, findings do not.** Spec validation raises
  `pydantic.ValidationError`; the diagnostic itself is a finding, with no
  envelope-level `error_code`.
- **Pure local read** of sidecars under `<experiment_dir>/.hpc/runs/`.

## Coupling

- Depends on `compute_cmd_sha` producing the same sha the submit path will
  compute; a change to the sha inputs on one side and not the other makes this
  gate compare the wrong thing.
- Depends on the framework's submit-time dedup semantics — this gate exists
  only because dedup is silent. If dedup ever became loud, the gate would be
  redundant rather than load-bearing.
- Reads the campaign's prior sidecars, so it inherits their retention: a
  pruned history cannot collide.

## Failure modes

- **Caller passes a stale `expected_cmd_sha`** → the gate compares a sha the
  submit will not actually use, and the collision slips through.
- **Prior iterations pruned or on another machine** → no sidecar, no
  collision detected, silent dedup returns.
- **Marker added but not inside `resolve()`'s returned dict** → it does not
  reach the sha inputs, so the collision persists while the user believes it
  is fixed.
