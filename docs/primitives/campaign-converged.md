---
name: campaign-converged
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-agent campaign-converged
  python: claude_hpc.atoms.campaign_converged.campaign_converged
---
# campaign-converged

> **Internal primitive.** Composed by `campaign-advance`; agents
> typically don't invoke directly.

Apply user-supplied stop criteria to a campaign's history. Pure
compute (no I/O beyond reading the campaign's history via
`campaign-replay`). Three independent triggers, ANY of which fires
returns `converged=True`:

- `max_iters` — iterations completed ≥ N
- `target` — best observed `metric` crosses a threshold (with
  `direction="minimize"` or `"maximize"`)
- `plateau_window` — best metric hasn't improved by more than
  `plateau_tolerance` in the last N iters

If no triggers are supplied, returns `converged=False` with
`reason="no_criteria"` (a no-op, by design — the framework holds
no opinion about defaults).

## Composers

- `campaign-advance` (composes `campaign-converged` +
  `campaign-budget` + `campaign-status` into a single
  decision).

## Invariants

- **Pure compute.** No filesystem or network side effects beyond
  whatever `campaign-replay` does to read history.
- **Triggers are independent.** Multiple triggers can match
  simultaneously; the function returns `converged=True` and a
  `reason` string identifying which one fired first (in declaration
  order: `max_iters` → `target` → `plateau_window`).
- **Empty / partial history**: a campaign with zero recorded
  metrics returns `converged=False, reason="no_metric_observed"`
  even when `max_iters=0` is supplied.

## Coupling

- Trigger semantics mirror `_schema_models/campaign_manifest.py:_StopCriteria`.
  Adding a new stop criterion means: extending the dataclass,
  updating the manifest schema, threading the kwarg through
  `campaign-converged` and `campaign-advance`, and surfacing the
  flag in `campaign-init`.
- The `direction` enum (`minimize`/`maximize`) is the public
  contract; flipping the default would invert every existing
  campaign's convergence criterion.

## Failure modes

- Metric-keyed history with non-numeric values silently skipped
  (`_extract_metric` filters non-`int`/`float`). A campaign whose
  metric drifted from `float` to `str` would be treated as having
  no observations.
- `plateau_window` larger than the recorded history → no plateau
  signal possible; returns `converged=False`. Caller should not
  interpret this as "still improving."
