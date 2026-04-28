#!/usr/bin/env python3
"""Standalone task dispatcher deployed to the HPC cluster.

This script is rsynced to the cluster and executed by the SGE job template.
It reads a JSON manifest to determine which command to run for the current
array task. It must remain zero-dependency — only Python stdlib, no imports
from the ``hpc_mapreduce`` package.
"""

import json
import os
import shutil
import subprocess
import sys
import time

__all__ = ["main"]

# Manifest schema versions this dispatcher accepts.  Kept in sync with
# ``MANIFEST_SCHEMA_VERSION`` in ``hpc_mapreduce/job/grid.py``.  Hardcoded
# here because this module must stay stdlib-only (it runs on cluster
# compute nodes without the ``hpc_mapreduce`` package installed).
#
# v1 and v2 are accepted: v2 adds per-task ``cmd_sha`` which the dispatcher
# does not need to consume — it's purely observational metadata for callers
# like ``/status``.  ``EXPECTED_SCHEMA_VERSION`` is kept as a module-level
# alias for the *current* canonical version (the one ``build_task_manifest``
# emits today) for existing tests that reference it.
SUPPORTED_SCHEMA_VERSIONS = (1, 2)
EXPECTED_SCHEMA_VERSION = SUPPORTED_SCHEMA_VERSIONS[-1]


def main() -> None:
    manifest_path = os.environ.get("HPC_MANIFEST", "_hpc_dispatch.json")

    # --- Load manifest ---
    if not os.path.isfile(manifest_path):
        print(f"[dispatch] ERROR: manifest not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
    except json.JSONDecodeError as exc:
        print(f"[dispatch] ERROR: failed to parse manifest: {exc}", file=sys.stderr)
        sys.exit(1)

    # --- Schema version check ---
    schema_version = manifest.get("schema_version")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        print(
            f"[dispatch] ERROR: manifest schema_version={schema_version}, "
            f"supported={list(SUPPORTED_SCHEMA_VERSIONS)}. Regenerate with "
            f"current hpc_mapreduce.",
            file=sys.stderr,
        )
        sys.exit(2)

    # --- Resolve task ---
    task_id = os.environ.get("TASK_ID")
    if task_id is None:
        print("[dispatch] ERROR: TASK_ID env var not set", file=sys.stderr)
        sys.exit(1)

    tasks = manifest.get("tasks", {})
    task = tasks.get(task_id)
    if task is None:
        valid = sorted(tasks.keys(), key=int)
        print(
            f"[dispatch] ERROR: task_id={task_id} not in manifest "
            f"(valid range: {valid[0]}–{valid[-1]})"
            if valid
            else f"[dispatch] ERROR: task_id={task_id} not in manifest (no tasks found)",
            file=sys.stderr,
        )
        sys.exit(1)

    cmd = task["cmd"]
    result_dir = task["result_dir"]

    # --- Execute (atomic output) ---
    # MapReduce correctness guarantee: write to a temporary work-in-progress
    # directory, then atomically promote files on success.  If the task
    # crashes mid-write, partial output stays in _wip_ and never pollutes
    # the final result directory.
    os.makedirs(result_dir, exist_ok=True)
    wip_dir = os.path.join(result_dir, f"_wip_{task_id}")

    # On retry of a previously-failed task, a stale _wip_{task_id}/ from the
    # prior attempt will still exist (we preserve it on failure, above).
    # Rename it aside so the new attempt starts from a clean slate, without
    # losing the prior partial output for forensic inspection.
    if os.path.isdir(wip_dir):
        stale_target = os.path.join(result_dir, f"_wip_{task_id}_failed_{int(time.time())}")
        try:
            os.rename(wip_dir, stale_target)
            print(f"[dispatch] preserved prior failed WIP at {stale_target}/")
        except OSError as exc:
            # Don't block the retry on a rename failure (permissions,
            # cross-device, etc.) — just continue.
            print(
                f"[dispatch] WARN: could not preserve stale WIP {wip_dir}: {exc}",
                file=sys.stderr,
            )

    os.makedirs(wip_dir, exist_ok=True)
    os.environ["RESULT_DIR"] = wip_dir

    print(f"[dispatch] task_id={task_id} cmd={cmd} result_dir={result_dir}")

    result = subprocess.run(cmd, shell=True, env=os.environ)

    if result.returncode == 0:
        # Promote: atomically move each output file to the final directory.
        for fname in os.listdir(wip_dir):
            os.replace(os.path.join(wip_dir, fname), os.path.join(result_dir, fname))
        shutil.rmtree(wip_dir, ignore_errors=True)
    else:
        print(
            f"[dispatch] FAILED (exit {result.returncode}), partial output preserved in {wip_dir}",
            file=sys.stderr,
        )

    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
