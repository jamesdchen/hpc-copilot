---
name: build-executor
verb: scaffold
side_effects:
- writes-file: <output_dir>/<name>.py (refuses to overwrite without --force)
idempotent: false
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
- code: config_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent build-executor --name <name> [--output-dir <output_dir>] [--type
    <type>] [--force]
  python: hpc_agent.incorporation.build.executor.build_executor
exit_codes:
- 0: ok
- 1: spec_invalid / config_invalid
---

## Purpose

Scaffold a new executor `.py` file from a starter template under `hpc_agent/templates/starters/`. The framework's only file-creation primitive — every other primitive is read-only or mutates only journal/sidecar files.

## Compose with

- Common predecessors: `discover-executors` (which returned an empty list, prompting the scaffold).
- Common successors: `discover-executors` (re-run to confirm the new file is recognized), then the standard submit pipeline.

## Notes

- The `plain` starter is currently the only type. Future types (e.g. `gpu`, `walk-forward`) would extend the enum here.
- Per-task fan-out lives in `.hpc/tasks.py` (separate file), not in the executor itself. The agent walks the user through writing `.hpc/tasks.py` after this primitive scaffolds the executor — that's surface logic in `/submit-hpc`.
