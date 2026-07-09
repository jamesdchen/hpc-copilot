---
name: dir-digest
verb: query
side_effects:
- ssh: '<cluster> when set: one read-only bounded digest probe'
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
- code: remote_command_failed
  category: cluster
  retry_safe: false
- code: cluster_unknown
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent dir-digest --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.dir_digest.dir_digest
---
# dir-digest

_Documentation pending._
