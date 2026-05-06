---
name: campaign-budget
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-mapreduce campaign-budget
  python: claude_hpc.atoms.campaign_budget.campaign_budget
---
# campaign-budget

> **Internal primitive.** Composed by `campaign-advance`; agents
> typically don't invoke directly.

Track spent vs. supplied budget caps. Pure read over sidecars
tagged with `campaign_id`. Sums `jobs` (number of completed run
sidecars), `tasks` (sum of `task_count` across completed runs),
`walltime_sec` (sum of per-task elapsed times if the
`last_status.tasks[*].elapsed_sec` field is observable). Returns
`exhausted=True` if any supplied cap is met.

## Composers

- `campaign-advance` (the agent's "should I continue?" decision
  primitive — see `docs/primitives/campaign-advance.md`).

No registered Python `composes=` references — `campaign-budget`
is invoked at the agent layer.

## Invariants

- **Pure read.** Walks `find_existing_runs(experiment_dir)` and
  filters by `campaign_id`. Never touches the journal, never
  rsyncs, never SSHs.
- **Caps come in as kwargs**, not the manifest. The manifest
  carries the canonical budget but `campaign-budget` accepts
  whatever the caller passes, so a strategy can override the
  manifest in flight (e.g. operator-pause).
- **Sidecars without `last_status` count toward `jobs` but not
  `walltime_sec`.** A run that completed before sidecar v2's
  `tasks` field existed reports `walltime_sec=0` — the rate
  estimate is conservative, not catastrophic.

## Coupling

- The budget shape (`max_jobs`, `max_tasks`, `max_walltime_sec`)
  is mirrored in `_schema_models/campaign_manifest.py:_CampaignBudget`
  and `campaign-init`'s flag set. Adding a budget axis means
  updating all three places.
- The "exhausted" semantic (any cap met → True) is a soft
  contract; loops should re-check after every `campaign-advance`
  before submitting.

## Failure modes

- Sidecar with no `campaign_id` → silently skipped (correct
  behavior — that run isn't part of any campaign).
- `walltime_sec` undercounts when a run's per-task `elapsed_sec`
  is missing (e.g. the cluster reporter never wrote it). No
  warning surfaced; consumer must trust `n_runs` more than
  `walltime_sec` for cold-start campaigns.
