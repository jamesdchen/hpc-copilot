---
name: campaign-list
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-mapreduce campaign list [--experiment-dir <dir>]
  python: claude_hpc.atoms.campaign_list.campaign_list
exit_codes:
- 0: ok
---

## Purpose

List every campaign with at least one tagged sidecar in `experiment_dir`, with iteration counts. Untagged (open-loop) sidecars are excluded.

## Compose with

- Common predecessors: none.
- Common successors: `campaign-status` (drill into one campaign), `submit-spec` (continue an existing campaign).

## Notes

Pure local sidecar walk; no SSH. Output validated against `schemas/campaign.output.json`'s `list_data`.
