---
name: submit-spec
verb: submit
side_effects:
- writes-journal: ~/.claude/hpc/<repo_hash>/runs/<run_id>.json
- scheduler-submit: <cluster>
idempotent: true
idempotency_key: spec.run_id
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
  description: Spec failed schema validation; fix the spec and retry.
- code: cluster_unknown
  category: user
  retry_safe: false
  description: spec.cluster does not match any clusters.yaml entry.
- code: ssh_unreachable
  category: network
  retry_safe: true
  description: SSH to the cluster failed. Re-run check-preflight; retry after fix.
- code: scheduler_throttled
  category: cluster
  retry_safe: true
  description: Scheduler rate limit hit. Wait ≥1s, retry the same spec (idempotency
    protects against double-submit).
backed_by:
  cli: hpc-agent submit --spec <path> [--experiment-dir <dir>] [--dry-run]
  python: claude_hpc.runner.submit.submit_and_record
exit_codes:
- 0: ok
- 1: user error (spec_invalid / cluster_unknown)
- 2: cluster/network (ssh_unreachable / scheduler_throttled — check retry_safe)
- 3: internal
---

## Purpose

Record one already-submitted (or about-to-be-submitted) cluster job in the journal and write the per-run sidecar. The sidecar carries identity (`run_id`, `cmd_sha`), the executor command, the result-dir template, and the wave map; it is the source of truth that every downstream primitive (`poll-run-status`, `aggregate-results`, `resubmit-failed`, `combine-wave`) reads.

This primitive does **not** call qsub / sbatch and does **not** rsync. The caller is expected to:

1. Have already rsync'd `experiment_dir` to `<ssh_target>:<remote_path>`.
2. Have already submitted the array job and captured the scheduler-assigned `job_ids`.
3. Pass those `job_ids` in the spec.

This separation exists because the qsub call itself is scheduler-specific (and the agent or human typically wants to inspect the qsub output before recording). `submit-spec` is the bookkeeping half; the qsub half lives in `slash_commands/runner.submit_plan` (or a future `dispatch-array-job` primitive).

## Idempotency

Replays with the same `run_id` are no-ops: the call returns `deduped=true` and does not re-issue qsub. The wrapper `claude_hpc.runner.submit_and_record` is keyed on `run_id` (which is itself derived from `cmd_sha`, so a re-run of an unchanged `.hpc/tasks.py` produces the same `run_id`). Callers who see `deduped=true` should switch to `poll-run-status` rather than re-running the upstream qsub.

## Compose with

Common predecessors:

- `check-preflight` — verifies SSH agent, ssh/rsync on PATH, clusters.yaml parses. Run before constructing any spec.
- `score-submit-plan` (`hpc-agent plan-submit`) — produces the constraint / walltime / exclude-list inputs that go into the spec.
- `discover-executors` — confirms `spec.profile` matches a real executor.

Common successors:

- `poll-run-status` (`hpc-agent status --run-id <id>`) — wait for terminal state.
- `aggregate-results` (`hpc-agent aggregate ...`) — once `lifecycle_state == complete`.
- `resubmit-failed` — if some tasks failed and the failure category is auto-recoverable.

## Surface composition

**Slash command (`/submit-hpc`)** wraps this primitive in:

- An interactive interview (which executor, which cluster, which parallelization axis) that builds the spec.
- A confirm-run-plan prompt before invocation.
- An optional canary submission that calls this primitive twice (once with `total_tasks=1` and `--no-canary` for the canary itself).
- Post-submission prompts ("monitor with /monitor-hpc?").

**Skill (`hpc-submit`)** wraps this primitive in:

- A precondition check (run `check-preflight` first if it has not been run this session).
- A construction step (build spec from caller args, defaulting where the slash command would prompt).
- A `--dry-run` invocation followed by the real invocation.
- Branching on `error_code` to either retry (`scheduler_throttled`) or surface to the caller.

Both surfaces invoke the same CLI / Python entry point. The contract above is the seam.

## Notes

- **SSH env passthrough**: any caller (slash command, skill, or downstream primitive) must forward `SSH_AUTH_SOCK` and `SSH_AGENT_PID` in the spawned env or the qsub-side hangs on auth. `check-preflight` catches this upfront.
- **Scheduler rate limits**: serialize submits to a single cluster; most schedulers cap at ~1/sec. Multiple back-to-back invocations of this primitive should sleep 1s between, or expect `scheduler_throttled`.
- **No cancel/abort**: claude-hpc has no kill primitive. If the caller decides an experiment is bad, stop monitoring; cluster jobs run to walltime.
- `--dry-run` never touches the cluster and never writes to the journal — safe to invoke repeatedly during spec construction.
