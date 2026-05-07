---
name: find-prior-run
verb: query
side_effects: []
idempotent: true
idempotency_key: cmd_sha
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent find-prior-run --experiment-dir <path> --cmd-sha <hex>
  python: claude_hpc.atoms.setup_actions.find_prior_run
exit_codes:
- 0: ok
- 1: user-error
---

## Purpose

Look up a prior run by `cmd_sha` for `/submit-hpc` Step 6c resume detection. Wraps `find_run_by_cmd_sha` + a sidecar read so the slash command routes through one CLI call instead of inline Python.

Returns `{found, run_id, is_orphan, status, age_sec, profile, cluster, job_ids, campaign_id, submitted_at}`. `found=False` when no sidecar matches; otherwise `is_orphan` distinguishes "real journal-wiped recovery candidate" from "half-baked sidecar from a failed batch."

## Compose with

- **Predecessors**: `compute-cmd-sha` (the agent computes the cmd_sha after `build-tasks-py`).
- **Successors**: `prune-orphan-sidecars` (when `is_orphan=True` and the user wants to clean up), `submit-flow` (when the user wants to fresh-submit anyway), `monitor-hpc` (when resuming the prior run).
