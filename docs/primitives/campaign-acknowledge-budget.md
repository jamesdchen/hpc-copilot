---
name: campaign-acknowledge-budget
verb: scaffold
side_effects:
- writes-sidecar: <experiment>/.hpc/campaigns/<id>/budget_ack.json
idempotent: true
idempotency_key: campaign_id
error_codes: []
backed_by:
  cli: hpc-agent campaign acknowledge-budget [--experiment-dir <dir>] --campaign-id
    <campaign_id> [--note <note>] [--max-jobs <max_jobs>] [--max-tasks <max_tasks>]
    [--max-walltime-sec <max_walltime_sec>] [--max-core-hours <max_core_hours>]
  python: hpc_agent.meta.campaign.atoms.acknowledge_budget.campaign_acknowledge_budget
---
# campaign-acknowledge-budget

Acknowledge a campaign budget halt so the loop may continue.

The budget governor (#224) makes `stop_over_budget` a halt the loop
**cannot** silently pass: once realised spend meets a cap,
[`campaign-advance`](campaign-advance.md) keeps returning
`stop_over_budget` (with `needs_acknowledgement: true`) until the
spend is explicitly acknowledged. This primitive writes that
acknowledgement to `<campaign_dir>/budget_ack.json`.

## Inputs

- `experiment_dir` (path) — repo root.
- `campaign_id` (str, required) — the campaign to acknowledge.
- `note` (str, default `""`) — free-form audit note recorded on the
  ack.
- Raised caps (all optional): `max_jobs`, `max_tasks`,
  `max_walltime_sec`, `max_core_hours`. Any supplied cap is merged
  into the manifest's `budget` section (existing caps and every other
  manifest section preserved) so `campaign-advance` reads the enlarged
  ceiling on the next tick.

## Outputs

`{campaign_id, acknowledged_spend, was_over_budget, raised_caps,
ack_path}`. `acknowledged_spend` is `campaign-budget`'s realised
`spent` block snapshotted into the ack; `was_over_budget` reflects the
budget state *after* any raised caps apply (raising a cap above current
spend clears the halt outright). Atomic write — partial writes are not
observable.

## Errors

None — a missing manifest is treated as empty (a minimal manifest is
created so raised caps are durable). The ack write never validates
against caps; it records spend as-is.

## Idempotency

Idempotent on `campaign_id`: re-acknowledging overwrites the prior ack
record, snapshotting spend at the latest call.

## Notes

- **Snapshot, not a blanket bypass.** Because spend is monotonic, the
  ack authorises continuing only while spend stays at the snapshot —
  the next task that burns compute makes it stale and
  `campaign-advance` re-arms the halt. A bare acknowledgement buys
  exactly one more leg; pass raised caps for durable headroom.
- **Conservative on corruption.** A malformed or missing ack reads as
  "no acknowledgement" in `campaign-advance`, so a corrupt ack can
  never relax the halt. The snapshot/stale semantics live in
  `hpc_agent.meta.campaign.budget_ack`; the halt decision lives in
  `campaign-advance`'s `_over_budget` rule.
