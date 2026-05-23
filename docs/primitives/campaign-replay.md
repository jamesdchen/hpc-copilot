---
name: campaign-replay
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent campaign replay [--experiment-dir <dir>] --campaign-id <campaign_id>
    [--last-n <last_n>]
  python: hpc_agent.atoms.campaign_replay.campaign_replay
---
# campaign-replay

> **Internal primitive.** Diagnostic helper composed by
> `campaign-converged` and used directly by debug tooling.

Return the last *N* iterations of a campaign, oldest-first. Each
iteration carries the sidecar's `run_id`, `submitted_at`,
`status`, and the reduced metrics dict produced by
`mapreduce.reduce.history.prior`. Iterations whose result
directories don't exist yet (still in flight) carry an empty
metrics dict.

## Composers

- `campaign-converged` (reads the history to evaluate stop
  criteria — see `docs/primitives/campaign-converged.md`).
- Bespoke debug tooling — `hpc-agent campaign-replay
  --campaign-id <id> --last-n 10` is useful for inspecting what a
  strategy actually did across recent steps.

## Invariants

- **Pure read.** Walks `find_sidecars_by_campaign(experiment_dir,
  campaign_id)` and reads the matching results dirs locally. No
  SSH, no journal mutation.
- **Oldest-first ordering** is the public contract. A reverse
  would silently break stop-criteria tests that walk the history
  forward looking for plateau windows.
- **In-flight tolerance**: missing or unreadable result dirs
  contribute an empty `metrics` dict, NOT an exception. The
  caller must distinguish "truly empty metrics" from "in flight"
  via `status`.

## Coupling

- The reduced-metrics dict shape is whatever
  `mapreduce.reduce.history.prior` emits — itself a thin wrapper
  around the experiment's reducer output. Changing reducer
  conventions cascades through `campaign-replay` →
  `campaign-converged` → `campaign-advance` to user-side strategy.
- `last_n` defaults to 5; the framework holds no opinion about
  larger windows. Callers needing the full history call with a
  large `last_n` and trust the sidecar count.

## Failure modes

- Campaign with zero matching sidecars → returns empty
  `iterations` list; loop should treat as "fresh, no signal yet."
- A run whose reducer crashed mid-write produces a partial
  metrics file; `prior` returns an empty dict. Same surface as
  "in flight" — distinguish via `status` (`failed` vs.
  `in_flight`).
