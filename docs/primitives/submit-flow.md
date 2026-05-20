---
name: submit-flow
verb: workflow
side_effects:
- sync-push: <ssh_target>:<remote_path>
- scheduler-submit: <cluster>
- writes-journal: ~/.claude/hpc/<repo_hash>/runs/<run_id>.json
idempotent: true
idempotency_key: run_id
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
- code: ssh_unreachable
  category: network
  retry_safe: true
- code: scheduler_throttled
  category: cluster
  retry_safe: true
- code: cluster_unknown
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent submit-flow --spec <path>
  python: claude_hpc.flows.submit_flow.submit_flow
---

## Purpose

**Workflow atom** that does the full submit pipeline as one CLI call: pre-flight + rsync + deploy framework files + optional canary + qsub + sidecar/journal write. Returns one envelope with `run_id`, `job_ids`, and `deduped` status.

Distinguished from the [submit-spec](submit-spec.md) primitive: that one is the bookkeeping atom (records a sidecar without touching the cluster). `submit-flow` is the end-to-end pipeline. Both are CLI atoms with the same envelope shape — that uniformity is what makes the campaign loop's `submit-flow → monitor-flow → aggregate-flow` chain composable.

Field-level contract: see `schemas/submit_flow.input.json` and `schemas/submit_flow.output.json`.

## Compose with

- Common predecessors: [check-preflight](check-preflight.md), [discover-executors](discover-executors.md), [score-submit-plan](score-submit-plan.md). Caller resolves which executor + which constraint + which walltime, then hands a fully-resolved spec to `submit-flow`.
- Common successors: [monitor-flow](monitor-flow.md) (poll the resulting `run_id` to terminal), [aggregate-flow](aggregate-flow.md) (combine + reduce per-wave partials).

## Notes

- **The interactive interview is NOT in the atom.** Executor-pick, parallelization-axis design, smart-planner judgment, run-plan confirmation — those live in `/submit-hpc` the slash command. The atom takes resolved inputs and executes.
- **Canary is fire-and-forget.** When `canary=true`, the atom submits a 1-task canary as a sibling sidecar and verifies qsub accepted it — but does NOT wait for the canary to complete or grep its logs. The "wait + verify outputs" canary protocol stays in `/submit-hpc` (interactive) or `/campaign-hpc` Path B's caller (programmatic).
- **Schedulers**: SGE via `RemoteSGEBackend`, SLURM via `RemoteSlurmBackend`. Both go through SSH; the local SGE/SLURM backends (which assume a local `qsub`/`sbatch` binary) are never used here. Plan-based wave/dependency submission is single-array in v1; multi-wave with dependencies is a future extension.
