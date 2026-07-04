---
name: verify-relay
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent verify-relay --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.decision.verify_relay.verify_relay
---
# verify-relay

_Documentation pending._
