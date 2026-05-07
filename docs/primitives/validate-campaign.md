---
name: validate-campaign
verb: workflow
side_effects: []
idempotent: true
idempotency_key: experiment_dir
error_codes: []
backed_by:
  cli: hpc-mapreduce validate-campaign --spec <path>
  python: claude_hpc.flows.validate_campaign.validate_campaign
---
# validate-campaign

_Documentation pending._
