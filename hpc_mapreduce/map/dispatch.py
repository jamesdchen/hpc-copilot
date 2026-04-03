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

__all__ = ["main"]


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

    # --- Resolve task ---
    chunk_id = os.environ.get("CHUNK_ID")
    if chunk_id is None:
        print("[dispatch] ERROR: CHUNK_ID env var not set", file=sys.stderr)
        sys.exit(1)

    tasks = manifest.get("tasks", {})
    task = tasks.get(chunk_id)
    if task is None:
        valid = sorted(tasks.keys(), key=int)
        print(
            f"[dispatch] ERROR: task_id={chunk_id} not in manifest "
            f"(valid range: {valid[0]}–{valid[-1]})"
            if valid
            else f"[dispatch] ERROR: task_id={chunk_id} not in manifest (no tasks found)",
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
    wip_dir = os.path.join(result_dir, f"_wip_{chunk_id}")
    os.makedirs(wip_dir, exist_ok=True)
    os.environ["RESULT_DIR"] = wip_dir

    print(f"[dispatch] task_id={chunk_id} cmd={cmd} result_dir={result_dir}")

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
