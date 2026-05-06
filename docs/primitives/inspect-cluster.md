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
  default: 0.8
- name: stress_cpu_load_frac
  type: float
  description: CPULoad/CPUTot fraction above which a node is `is_stressed=true`.
  default: 0.8
- name: no_cache
  type: bool
  description: Bypass the 60s in-process snapshot cache.
  default: false
side_effects:
- ssh: <cluster>
idempotent: true
idempotency_key: cluster
error_codes:
- code: cluster_unknown
  category: user
  retry_safe: false
- code: ssh_unreachable
  category: network
  retry_safe: true
backed_by:
  cli: hpc-mapreduce inspect-cluster --cluster <name> [...]
  python: claude_hpc.infra.inspect.inspect_cluster
exit_codes:
- 0: ok
- 2: ssh_unreachable
- 3: cluster_unknown / internal
---
# inspect-cluster

> **Internal primitive.** Composed by `score-submit-plan` (the
> agent-facing planner). Direct invocation is fine for ad-hoc
> cluster debugging; the typical hot path goes through
> `plan-submit` instead.

Per-node cluster snapshot: alloc-mem pressure, CPU load,
advertised vs. allocated GRES, drain/down flags, co-tenant list
from `sacct -N` / `qstat`. Read-only.

## Composers

- `score-submit-plan` (uses this snapshot as one of its scoring
  inputs).
- `verify-canary` and `monitor-flow` consult cluster state
  indirectly via `record_status` — they do NOT call
  `inspect-cluster` directly; cluster-level snapshots happen on
  the planner side, run-level status on the runner side.

## Invariants

- **One SSH per cluster per 60s.** In-process snapshot cache
  keyed on `(cluster_name, scheduler)`. Back-to-back invocations
  during a single submit cycle are free.
- **Stress heuristics are tunable** via `stress_alloc_mem_pct`
  and `stress_cpu_load_frac` kwargs (defaults 0.80). Callers
  that want a different load profile pass overrides; the
  framework holds no opinion on canonical thresholds.
- **`is_stressed` and `is_drained` are per-node booleans.** A
  consumer that wants "fraction of healthy nodes" computes it
  from the list — the snapshot doesn't pre-aggregate.

## Coupling

- Backend dispatch goes through `infra.backends.get_backend_class`.
  Adding a new scheduler means: new backend module + a new
  `inspect_cluster` classmethod on the backend that produces the
  same `ClusterSnapshot` shape this atom expects. The Pydantic
  `_NodeSnapshot` model in `_schema_models/inspect_cluster.py`
  documents the wire shape.
- `data.errors` is the soft-failure bag (one node failed to
  report). Fatal cases (whole cluster unreachable) raise
  `SshUnreachable` instead. Renaming or restructuring `errors`
  is a wire-breaking change.

## Failure modes

- Stale cluster.yaml ssh_target → `SshUnreachable` propagates;
  nothing partial returned. Caller should check `clusters-describe`
  first if validating a fresh entry.
- A cluster that exposes neither `qhost` (SGE) nor `sinfo`
  (SLURM) successfully → empty `nodes` list with descriptive
  `errors` entries. Consumers must handle `nodes=[]` (no panic;
  no scoring possible).
