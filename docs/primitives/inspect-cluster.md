---
name: inspect-cluster
verb: query
inputs:
  - name: cluster
    type: string
    description: Cluster key from clusters.yaml.
  - name: sacct_window_hours
    type: int
    description: Co-tenant look-back window.
    default: 24
  - name: stress_alloc_mem_pct
    type: float
    description: AllocMem fraction above which a node is `is_stressed=true`.
    default: 0.80
  - name: stress_cpu_load_frac
    type: float
    description: CPULoad/CPUTot fraction above which a node is `is_stressed=true`.
    default: 0.80
  - name: no_cache
    type: bool
    description: Bypass the 60s in-process snapshot cache.
    default: false
side_effects:
  - ssh: cluster reachable on submit cluster
  - cache: in-process snapshot cache (60s TTL by default)
idempotent: true
idempotency_key: cluster (within cache window)
error_codes:
  - code: ssh_unreachable
    category: network
    retry_safe: true
  - code: cluster_unknown
    category: user
    retry_safe: false
  - code: internal
    category: internal
    retry_safe: false
backed_by:
  cli: hpc-mapreduce inspect-cluster --cluster <name> [...]
  python: hpc_mapreduce.infra.inspect.inspect_cluster
exit_codes:
  - 0: ok
  - 2: ssh_unreachable
  - 3: cluster_unknown / internal
---

## Purpose

Read-only snapshot of a cluster's per-node state — alloc-mem pressure, CPU load, advertised vs. allocated GRES, drain/down flag, plus a co-tenant list from `sacct -N` / `qstat`. Used by `score-submit-plan` and useful standalone for ad-hoc cluster debugging.

## Compose with

- Common predecessors: `clusters-describe` (to confirm the cluster name exists).
- Common successors: `score-submit-plan` (which calls this primitive internally as one of its inputs).

## Notes

- `is_stressed` is a heuristic boolean; tune via the `stress_*` params when defaults don't match a cluster's load profile.
- The 60s cache means back-to-back invocations on the same cluster are free — useful when the agent wants to check both "is this cluster healthy" and "what does plan-submit say" without paying SSH twice.
- Errors collected in `data.errors` are non-fatal (e.g. one node failed to report); fatal cases raise `ssh_unreachable`.
