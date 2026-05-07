---
name: update-run-constraints
verb: mutate
side_effects:
- ssh: <cluster> (scontrol update Features)
idempotent: true
idempotency_key: run_id
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
- code: ssh_unreachable
  category: network
  retry_safe: true
- code: remote_command_failed
  category: cluster
  retry_safe: false
backed_by:
  cli: hpc-mapreduce update-run-constraints --spec <path>
  python: claude_hpc.runner.update_constraints.update_run_constraints
---
# update-run-constraints

_Documentation pending._
