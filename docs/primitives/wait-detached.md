---
name: wait-detached
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent wait-detached --spec <path>
  python: hpc_agent.ops.monitor.wait_detached.wait_detached
---
# wait-detached

_Documentation pending._
