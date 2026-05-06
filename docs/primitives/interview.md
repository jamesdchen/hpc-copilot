---
name: interview
verb: scaffold
side_effects:
- file_write: <campaign_dir>/{interview.json,meta.json}
idempotent: true
idempotency_key: campaign_dir
error_codes: []
backed_by:
  cli: hpc-mapreduce interview
  python: claude_hpc.atoms.interview.record_interview
---
# interview

_Documentation pending._
