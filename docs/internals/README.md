# Internals

Documents that live in this directory are for framework maintainers — design
notes, recipes for adding internals, and architecture deep-dives. They are
**not** the agent surface (see [`docs/primitives/`](../primitives/) and
[`docs/reference/`](../reference/) for that).

## Index

| Doc | Purpose |
|---|---|
| [`adding-a-primitive.md`](adding-a-primitive.md) | Step-by-step recipe for landing a new wire-surface primitive (atom or workflow). |
| [`queue-wait-predictor.md`](queue-wait-predictor.md) | Operational notes on the LightGBM queue-wait residual model: training data, retraining cadence, calibration loop. |
| [`queue-wait-predictor-architecture.md`](queue-wait-predictor-architecture.md) | Architecture deep-dive on the floor + residual design (FIFO/backfill simulators + LGBM). Pairs with `queue-wait-predictor.md`. |
| [`sync-checklist.md`](sync-checklist.md) | Invariants between the slash-command surface and the `hpc-mapreduce` CLI — what must stay aligned when either changes. |

## When to add a doc here

- Architecture or algorithm design that a maintainer would need to understand
  before changing the implementation.
- Recipes / playbooks for repeated maintenance tasks.
- Cross-cutting invariants that span multiple subpackages.

If the doc is for a primitive caller, it belongs in `docs/primitives/` (one
file per primitive) or `docs/reference/` (cross-cutting wire contracts).
