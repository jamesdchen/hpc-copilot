---
name: campaign-init
verb: scaffold
side_effects:
- writes-sidecar: <experiment>/.hpc/campaigns/<id>/manifest.json
idempotent: true
idempotency_key: campaign_id
error_codes: []
backed_by:
  cli: hpc-mapreduce campaign-init
  python: claude_hpc.atoms.campaign_init.campaign_init
---
# campaign-init

_Documentation pending._
