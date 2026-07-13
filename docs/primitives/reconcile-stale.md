---
name: reconcile-stale
verb: mutate
side_effects:
- writes-journal: ~/.claude/hpc/<repo_hash>/runs/<run_id>.json (under flock) — terminal
    close for scheduler-unknown in-flight runs
- ssh: <cluster> (one scheduler-state query per login node, via batch-status)
idempotent: true
idempotency_key: experiment_dir
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent reconcile-stale [--experiment-dir <dir>] [--now <now>] [--stale-after-hours
    <stale_after_hours>]
  python: hpc_agent.ops.monitor.reconcile_stale.reconcile_stale
---
# reconcile-stale

Bulk closure for stale in-flight journal records — runs whose jobs left the
scheduler without a terminal write (run #11: 35 phantom `in_flight` records
from a revoked cluster account slowed every unscoped surface for weeks).

Gathers the experiment's non-terminal RunRecords, groups by cluster, makes
ONE `batch-status` scheduler query per login node (never per-run SSH), and
closes each run whose recorded job_ids are all scheduler-unknown through the
EXISTING reconcile classification (`abandoned` / `no_on_disk_evidence` —
never a status invented here). Jobless records close only past a staleness
threshold (default 24h). Anything ambiguous — scheduler still knows a job,
young jobless records, un-batchable backends, or an unreachable cluster —
stays open and is listed. Closure is journal-only (no harvest, no cluster
action). The result carries a code-rendered summary: examined, queries,
closed-by-class, left-open with reasons.

Origin: run #11 queue item 11 (`docs/design/notebook-audit.md`, Addendum 6).
