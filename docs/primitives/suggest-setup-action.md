---
name: suggest-setup-action
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent suggest-setup-action --experiment-dir <path>
  python: claude_hpc.atoms.setup_actions.suggest_setup_action
exit_codes:
- 0: ok
- 1: user-error
---

## Purpose

Run the `/submit-hpc` Setup priority cascade and recommend the next action. Replaces the priority-list-walking prose at Step 0 with a deterministic primitive.

Priority order (matches the slash command):

* **0 — `monitor`**: in-flight runs exist on the journal. Hand off to `/monitor-hpc`.
* **1 — `reuse`**: per-experiment sidecars exist. Offer recent (profile, cluster) pairs as "same as last".
* **2 — `interview`**: `.hpc/tasks.py` exists but no run history. Skip executor discovery + axes interview; jump to the planner.
* **3 — `fresh`**: nothing exists. Full interview from Step 1.

Returns `{priority, action, run_id, candidates, reason}`. Agent branches on `action` and surfaces `candidates` to the user.

## Compose with

- **Predecessors**: none — this is the entrypoint primitive for `/submit-hpc` Setup.
- **Successors**: `find-prior-run` (resume detection within priority 1), `build-tasks-py` (priority 2/3 path), `interview` atom (priority 3 path).
