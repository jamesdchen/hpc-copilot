---
name: verify-canary
verb: workflow
side_effects:
- ssh: <cluster> (poll status + tail stderr)
idempotent: true
idempotency_key: canary_run_id
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
- code: ssh_unreachable
  category: network
  retry_safe: true
backed_by:
  cli: hpc-agent verify-canary [--experiment-dir <dir>] --canary-run-id <canary_run_id>
    [--expect-output <expect_output>] [--poll-interval-sec <poll_interval_sec>] [--wait-budget-sec
    <wait_budget_sec>]
  python: hpc_agent.atoms.canary_verify.verify_canary
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

## Notes

- **`failure_kind` is one of**: `dispatcher_failed`, `import_error`, `module_not_found`, `traceback`, `oom_killed`, `segfault`, `missing_output`, `timeout`, `abandoned`. Specific markers ranked first so an `ImportError` doesn't get reported as a generic `traceback`.
- **Composes `record-status`** (the primitive does the polling via `_ssh_status_report`).
- **Idempotent**: re-running on a terminal canary returns the same result without re-polling indefinitely.
- **Default budget 1800s (30 min)** — long enough for a 1-task probe through a busy queue; configurable via `--wait-budget-sec`.
