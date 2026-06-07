---
name: verify-canary
verb: workflow
side_effects:
- ssh: <cluster> (poll status + tail stderr)
idempotent: true
idempotency_key: canary_run_id
error_codes:
- &id001
  code: spec_invalid
  category: user
  retry_safe: false
- code: ssh_unreachable
  category: network
  retry_safe: true
- *id001
backed_by:
  cli: hpc-agent verify-canary [--experiment-dir <dir>] --canary-run-id <canary_run_id>
    [--expect-output <expect_output>] [--fingerprint <fingerprint>] [--verify-checkpoint]
    [--checkpoint-result-dir <checkpoint_result_dir>] [--poll-interval-sec <poll_interval_sec>]
    [--wait-budget-sec <wait_budget_sec>]
  python: hpc_agent.ops.verify_canary.verify_canary
exit_codes:
- 0: ok
- 1: user-error
- 2: cluster
---

## Purpose

Wait + grep + output-check protocol for a 1-task canary submission. Replaces the multi-step prose at `/submit-hpc` Step 7b/8 — the most fragile multi-step protocol in the slash command — with one workflow atom.

The atom:

1. Polls the run record until terminal (or wait_budget elapses).
2. Greps the canary's stderr log for known failure markers (`[dispatch] FAILED`, `Traceback`, `ImportError`, `ModuleNotFoundError`, `Out of memory`, `Segmentation fault`).
3. Optionally verifies an expected output artifact exists in the canary's result_dir.

Returns `{ok, failure_kind, details, stderr_tail}`. Caller branches on `ok`: True → main array submit; False → surface `stderr_tail` to the user verbatim (don't paraphrase).

## Compose with

- **Predecessors**: `submit-flow` with `canary=true` (creates the canary run record this primitive polls against).
- **Successors**: agent decides whether to proceed with the main array based on `ok`.

## Checkpoint canary (`--verify-checkpoint`, #294 PR4)

A run that opts into `auto_resume_on_kill` checkpoint-resume needs to prove its checkpoint format actually round-trips **before** the long main array launches — otherwise it discovers an unreloadable checkpoint only at resume time, hours in. `submit-flow` stamps that run's canary with `HPC_CHECKPOINT_CANARY=1`; an executor driving its loop through `experiment_kit.checkpoint.run_iterations` then writes a checkpoint at iteration 1 and kills itself at iteration 2 via the dispatcher's real SIGTERM preemption path.

With `--verify-checkpoint`, this atom **swaps its success criteria**: a preempted canary (exit 130) is the *expected* outcome, so the exit-0 / output checks don't apply. Instead it runs `read_latest_checkpoint` on the cluster (under the run's conda activation, where a resume would actually reload it) against the canary's `_checkpoints/` dir and passes iff a **loadable** checkpoint survived. The checkpoint dir is derived from the canary sidecar's `result_dir_template` (task 0); pass `--checkpoint-result-dir` when that template references per-task kwargs that can't be rendered locally. `submit-and-verify` wires all of this automatically when `submit.auto_resume_on_kill` is set.

## Notes

- **`failure_kind` is one of**: `dispatcher_failed`, `import_error`, `module_not_found`, `traceback`, `oom_killed`, `segfault`, `missing_output`, `timeout`, `abandoned`. Specific markers ranked first so an `ImportError` doesn't get reported as a generic `traceback`. In checkpoint mode (`--verify-checkpoint`): `checkpoint_missing` (no checkpoint written before the kill) or `checkpoint_unloadable` (a checkpoint exists but `read_latest_checkpoint` can't reload it — a wrong/non-portable format).
- **Composes `record-status`** (the primitive does the polling via `_ssh_status_report`).
- **Idempotent**: re-running on a terminal canary returns the same result without re-polling indefinitely.
- **Default budget 1800s (30 min)** — long enough for a 1-task probe through a busy queue; configurable via `--wait-budget-sec`.
