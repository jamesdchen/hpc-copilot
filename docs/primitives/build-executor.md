---
name: build-executor
verb: scaffold
inputs:
  - name: name
    type: string
    description: Output filename stem (no .py).
  - name: output_dir
    type: path
    description: Where to write the new file. Defaults to cwd.
  - name: type
    type: enum
    description: Starter template selector. Currently only `plain`.
    default: plain
  - name: force
    type: bool
    description: Overwrite existing destination.
    default: false
side_effects:
  - writes: <output_dir>/<name>.py
idempotent: false
idempotency_key: none — file creation has side effects; refuses to overwrite without force=true
error_codes:
  - code: spec_invalid
    category: user
    retry_safe: false
    description: Unknown type, or destination exists and force=false.
  - code: config_invalid
    category: user
    retry_safe: false
    description: Starter template missing on disk (corrupted install).
backed_by:
  cli: hpc-mapreduce build-executor --name <stem> [--output-dir <dir>] [--type plain] [--force]
  python: hpc_mapreduce.agent_cli.cmd_build_executor
exit_codes:
  - 0: ok
  - 1: spec_invalid / config_invalid
---

## Purpose

Scaffold a new executor `.py` file from a starter template under `hpc_mapreduce/templates/starters/`. The framework's only file-creation primitive — every other primitive is read-only or mutates only journal/sidecar files.

## Compose with

- Common predecessors: `discover-executors` (which returned an empty list, prompting the scaffold).
- Common successors: `discover-executors` (re-run to confirm the new file is recognized), then the standard submit pipeline.

## Notes

- The `plain` starter is currently the only type. Future types (e.g. `gpu`, `walk-forward`) would extend the enum here.
- Per-task fan-out lives in `.hpc/tasks.py` (separate file), not in the executor itself. The agent walks the user through writing `.hpc/tasks.py` after this primitive scaffolds the executor — that's surface logic in `/submit-hpc`.
