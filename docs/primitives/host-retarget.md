---
name: host-retarget
verb: mutate
side_effects:
- writes-journal: <experiment>/.hpc/decisions/run/<run_id>.jsonl (the failover decision)
    + the run record's cluster/ssh_target (locked update_run_record)
idempotent: true
idempotency_key: run_id
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
- code: cluster_unknown
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent host-retarget --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.host_retarget.host_retarget
---
# host-retarget

Move an **in-flight** run to a different login node of the **same** cluster by
patching the run record's one `cluster` key as a journaled decision (run-12
finding 23; the sanctioned expression of RULING 1). `resolve_ssh_target` already
derives `user@host` from that key at use time, so every transport consumer picks
up the new login node with no journal surgery — the exact hand-edit the relay
agent had to improvise when discovery2 fork-exhausted and discovery1 was healthy.

This is **not** `retarget-run`. `retarget-run` mints a new run_id, supersedes the
failed attempt, and re-canaries — for a genuine cluster *move* where the jobs
re-stage. `host-retarget` keeps the same run, run_id, job_ids, scratch, and
scheduler, and moves only the login node — so it refuses any new cluster that does
not serve the same scheduler and scratch.

## Inputs

- `run_id` (str) — the in-flight run to re-point.
- `cluster` (str) — the new cluster KEY (a `clusters.yaml` entry for the healthy
  login node). Must serve the same scheduler and scratch as the run's current
  cluster.
- `reason` (str, optional) — human rationale journaled with the decision.

## Outputs

`{stage_reached: "host_retargeted", run_id, old_cluster, new_cluster,
old_ssh_target, new_ssh_target, decision_ts, reason}` — the audit of the failover
(the run's identity and jobs are unchanged).

## Errors

- `spec_invalid` — no such run; a same-cluster no-op; or the new cluster serves a
  different scheduler/scratch (a cluster move — use `retarget-run`).
- `cluster_unknown` — the new cluster is absent from `clusters.yaml` or yields no
  derivable `user@host`.

## Idempotency

Keyed on `run_id`. Re-pointing to the cluster the run is already on is refused (a
same-key host change is a plain `clusters.yaml` edit, nothing per-run to journal).

## Notes

Patches the record through the sanctioned locked `update_run_record` callback
(the `cluster` key is deliberately outside `update_run_status`'s whitelist — it is
an identity/provenance field). The record's `ssh_target` is updated to the new
live value so provenance stays coherent.
