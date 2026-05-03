---
name: clusters-describe
verb: query
inputs:
  - name: name
    type: string
    description: Cluster key from clusters.yaml.
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
  - code: cluster_unknown
    category: user
    retry_safe: false
  - code: config_invalid
    category: user
    retry_safe: false
backed_by:
  cli: hpc-mapreduce clusters describe <name>
  python: hpc_mapreduce.agent_cli.cmd_clusters_describe
exit_codes:
  - 0: ok
  - 1: cluster_unknown / config_invalid
---

## Purpose

Print one cluster's full config block. Use to inspect what `--cluster <name>` will resolve to before invoking a side-effecting primitive against that cluster.

## Compose with

- Common predecessors: `clusters-list` (to discover names).
- Common successors: `submit-spec`, `inspect-cluster`, `score-submit-plan`.

## Notes

Pure local config read; no SSH.
