---
name: recommend-wait-alternative
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: (none — Python-only primitive)
  python: claude_hpc.atoms.recommend_wait_alternative.recommend_wait_alternative
---
# recommend-wait-alternative

Quantify the "do nothing" alternative: fit a climb rate from priority samples and forecast how much your priority will climb under each requested wait horizon. The agent can then compare "submit now with predicted wait W" against "wait H hours, priority climbs to P, then submit." Returns the fitted rate and forecasts for each horizon so the agent has both arms to compare.

## Inputs

- `current_priority` (integer) — Your pending job's current priority on the target partition.
- `samples` (list of objects, default `[]`) — Past `(observed_at_iso, priority)` observations from pending jobs on the same partition. Each sample has:
  - `observed_at_iso` (string) — ISO timestamp of the observation.
  - `priority` (integer) — Priority value at that timestamp.
  - Two or more samples enable OLS regression; fewer returns `method=insufficient_data` and a zero-rate forecast.
- `wait_horizons_hours` (list of floats, default `[1.0, 3.0, 6.0, 12.0, 24.0]`) — Wait durations (hours) to forecast climbed priority for.

## Outputs

A `RecommendWaitAlternativeResult` object with:

- `rate_priority_per_hour` (float) — Fitted climb rate (priority points per hour). Zero when `method` is `insufficient_data` or `reset_observed`.
- `method` (string) — One of: `"linear_regression"` (3+ samples, OLS fit), `"two_point"` (exactly 2 samples, single-segment estimate), `"insufficient_data"` (<2 samples), `"reset_observed"` (priority went down between samples — resubmit or reservation pinned).
- `n_samples` (integer) — Number of samples used.
- `forecasts` (list of objects) — One entry per requested horizon. Each has:
  - `wait_hours` (float) — The horizon.
  - `forecast_priority` (integer) — Predicted priority if you wait this long.

## Errors

None declared. Spec validation errors raise `pydantic.ValidationError`; with fewer than two samples the primitive still returns a result with `method="insufficient_data"` and an empty `forecasts` list — sparse-data signalling is structured, not an error.

## Idempotency

Pure local fitting — calling twice with the same samples produces the same result.

## Notes

- `method=insufficient_data` surfaces "no trust signal" to the agent without forcing it to reason about missing data itself. The `forecasts` list is empty in this case.
- `method=reset_observed` flags a priority reset (e.g., job resubmitted, fairshare reservation reset). The fitted rate is clamped to zero; the agent should not rely on the forecast.
- The forecasts are linear: `forecast_priority(h) = current_priority + rate_priority_per_hour * h`. Non-linear effects (e.g., fairshare decay) are not modeled.
- For large samples (3+ observations), OLS regression is used; for exactly 2, a single segment is fitted; for <2, the rate is zero and method signals the limitation.

**Schemas:** [`recommend_wait_alternative.input.json`](../../src/claude_hpc/schemas/recommend_wait_alternative.input.json), [`recommend_wait_alternative.output.json`](../../src/claude_hpc/schemas/recommend_wait_alternative.output.json).
