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
- code: remote_command_failed
  category: cluster
  retry_safe: false
- code: cluster_unknown
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent submit-flow --spec <path> [--experiment-dir <dir>] [--dry-run] [--partial-ok]
    [--invalidate-on-code-change]
  python: hpc_agent.ops.submit_flow.submit_flow
---

## Purpose

**Workflow atom** that does the full submit pipeline as one CLI call: pre-flight + rsync + deploy framework files + optional canary + qsub + sidecar/journal write. Returns one envelope with `run_id`, `job_ids`, `deduped`, `main_launched`, and canary status.

Distinguished from the [submit-spec](submit-spec.md) primitive: that one is the bookkeeping atom (records a sidecar without touching the cluster). `submit-flow` is the end-to-end pipeline. Both are CLI atoms with the same envelope shape — that uniformity is what makes the campaign loop's `submit-flow → monitor-flow → aggregate-flow` chain composable.

Field-level contract: see `schemas/submit_flow.input.json` and `schemas/submit_flow.output.json`.

## Compose with

- Common predecessors: [check-preflight](check-preflight.md), [discover-executors](discover-executors.md), [score-submit-plan](score-submit-plan.md). Caller resolves which executor + which constraint + which walltime, then hands a fully-resolved spec to `submit-flow`.
- Common successors: [monitor-flow](monitor-flow.md) (poll the resulting `run_id` to terminal), [aggregate-flow](aggregate-flow.md) (combine + reduce per-wave partials).

## Notes

- **The interactive interview is NOT in the atom.** Executor-pick, parallelization-axis design, smart-planner judgment, run-plan confirmation — those live in `/submit-hpc` the slash command. The atom takes resolved inputs and executes.
- **Canary modes.** With `canary=true` (default), the atom submits a 1-task canary as a sibling sidecar (mirroring the run's per-task executor, #162a) alongside the main array and only checks that qsub accepted it — fire-and-forget. With `canary_only=true` (#160), it submits ONLY the canary and returns `main_launched=false`; the caller then runs `verify-canary` and re-invokes with `canary=false` to launch the main array **only on a verified canary**. The "wait + verify outputs" protocol itself lives in `verify-canary` (used by `/submit-hpc`, `submit-and-verify`, and `/campaign-hpc` Path B).
- **Schedulers**: SGE via `RemoteSGEBackend`, SLURM via `RemoteSlurmBackend`. Both go through SSH; the local SGE/SLURM backends (which assume a local `qsub`/`sbatch` binary) are never used here. Plan-based wave/dependency submission is single-array in v1; multi-wave with dependencies is a future extension.
- **Batch spec auto-routing.** A spec of the form `{"specs": [<per-spec>...], "rsync_excludes": [...], "skip_preflight": ...}` is auto-routed to `submit-flow-batch`: one rsync + one deploy + N qsubs over the multiplexed ssh ControlMaster. This exists because N parallel single-spec submits send ~13×N ssh handshakes at the cluster's sshd and trip `MaxStartups`. All per-specs must share `(ssh_target, remote_path)`; a heterogeneous batch raises `spec_invalid`.
- **Cluster-side `.hpc/` is `--delete`-protected.** The framework files `deploy_runtime` places inside the cluster's `.hpc/` (`_hpc_dispatch.py`, `_hpc_combiner.py`, `templates/`) are protected from the rsync `--delete` via `DEFAULT_RSYNC_EXCLUDES` in `hpc_agent.infra.remote`, so the caller's rsync excludes need only cover repo content (`.gitignore` patterns, caches, result dirs) — not the deployed runtime.
