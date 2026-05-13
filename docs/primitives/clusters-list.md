---
name: clusters-list
verb: query
inputs: []
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: config_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent clusters list
  python: claude_hpc.atoms.clusters.list_clusters
exit_codes:
- 0: ok
- 1: config_invalid
---

## Purpose

List every cluster defined in the active `clusters.yaml`. The discovery step before any operation that names a cluster.

## Compose with

- Common predecessors: none.
- Common successors: `clusters-describe`, `submit-spec`, `inspect-cluster`, `score-submit-plan`.

## Notes

Pure local config read; no SSH. Source resolved via the documented config-precedence order (see `docs/reference/config-precedence.md`).
