#!/usr/bin/env python3
"""Standalone task dispatcher deployed to the HPC cluster.

This script is scp'd to ``$REMOTE_PATH/.hpc/_hpc_dispatch.py`` by
``deploy_runtime`` and executed by the SGE/SLURM array-job template.

Per-task identity comes from ``HPC_TASK_ID``; per-run identity from
``HPC_RUN_ID``. The dispatcher:

1. Imports the user's ``.hpc/tasks.py`` (sibling of this file) and calls
   ``resolve(task_id)`` to get the per-task kwargs.
2. Reads the per-run sidecar at ``.hpc/runs/<HPC_RUN_ID>.json`` for the
   executor command and result-directory template.
3. Formats the result directory using kwargs + ``task_id``.
4. Sets the kwargs as env vars (both raw uppercase and ``HPC_KW_*``).
5. Runs the executor in a shell with WIP / atomic-promote semantics so
   crashed mid-runs never pollute the final result directory.

Stays zero-dependency — only Python stdlib, no imports from the
``claude_hpc`` package.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

__all__ = ["main"]

# Sidecar schema versions this dispatcher accepts. Kept in sync with
# ``SIDECAR_SCHEMA_VERSION`` in ``claude_hpc/orchestrator/runs.py``. Hardcoded
# here because this module must stay stdlib-only.
#
# v2 added optional fields (wave_map, aggregate_defaults, ...). The
# dispatcher reads only the v1-shape fields (sidecar_schema_version,
# executor, result_dir_template), so accepting v2 here is safe; the
# extra fields are simply ignored cluster-side.
SUPPORTED_SCHEMA_VERSIONS = (1, 2)


def _load_tasks_module(tasks_py_path):
    spec = importlib.util.spec_from_file_location("hpc_user_tasks", tasks_py_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load tasks.py from {tasks_py_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _format_result_dir(template, *, task_id, run_id, kwargs):
    """Render *template* using ``str.format`` with task/run identity + kwargs.

    Kwargs win over reserved keys on collision, matching the documented
    behaviour: the user's tasks.py controls the namespace.
    """
    ctx = {"task_id": task_id, "run_id": run_id, **kwargs}
    try:
        return template.format(**ctx)
    except KeyError as exc:
        raise KeyError(
            f"result_dir_template references unknown key {exc.args[0]!r}; available: {sorted(ctx)}"
        ) from None


def main() -> None:
    here = Path(__file__).resolve().parent  # cluster-side .hpc/

    # --- Load user tasks.py ---
    tasks_path_str = os.environ.get("HPC_TASKS_PATH")
    tasks_path = Path(tasks_path_str) if tasks_path_str else here / "tasks.py"
    if not tasks_path.is_file():
        print(f"[dispatch] ERROR: tasks.py not found: {tasks_path}", file=sys.stderr)
        sys.exit(1)
    try:
        tasks = _load_tasks_module(tasks_path)
    except Exception as exc:
        print(f"[dispatch] ERROR: failed to import tasks.py: {exc}", file=sys.stderr)
        sys.exit(1)
    if not (hasattr(tasks, "total") and hasattr(tasks, "resolve")):
        print(
            f"[dispatch] ERROR: {tasks_path} must define total() and resolve(task_id)",
            file=sys.stderr,
        )
        sys.exit(1)

    # --- Resolve task identity ---
    task_id_str = os.environ.get("HPC_TASK_ID") or os.environ.get("TASK_ID")
    if task_id_str is None:
        print("[dispatch] ERROR: HPC_TASK_ID env var not set", file=sys.stderr)
        sys.exit(1)
    try:
        task_id = int(task_id_str)
    except ValueError:
        print(f"[dispatch] ERROR: HPC_TASK_ID is not an integer: {task_id_str!r}", file=sys.stderr)
        sys.exit(1)

    n = int(tasks.total())
    if not 0 <= task_id < n:
        print(
            f"[dispatch] ERROR: task_id={task_id} out of range [0, {n})",
            file=sys.stderr,
        )
        sys.exit(1)

    # --- Resolve run identity & sidecar ---
    run_id = os.environ.get("HPC_RUN_ID")
    if not run_id:
        print("[dispatch] ERROR: HPC_RUN_ID env var not set", file=sys.stderr)
        sys.exit(1)

    sidecar_path = here / "runs" / f"{run_id}.json"
    if not sidecar_path.is_file():
        print(f"[dispatch] ERROR: run sidecar not found: {sidecar_path}", file=sys.stderr)
        sys.exit(1)
    try:
        sidecar = json.loads(sidecar_path.read_text())
    except json.JSONDecodeError as exc:
        print(f"[dispatch] ERROR: failed to parse sidecar: {exc}", file=sys.stderr)
        sys.exit(1)

    schema_version = sidecar.get("sidecar_schema_version")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        print(
            f"[dispatch] ERROR: sidecar schema_version={schema_version}, "
            f"supported={list(SUPPORTED_SCHEMA_VERSIONS)}. Re-submit with current claude-hpc.",
            file=sys.stderr,
        )
        sys.exit(2)

    executor = sidecar.get("executor")
    result_dir_template = sidecar.get("result_dir_template")
    if not executor or not result_dir_template:
        print(
            "[dispatch] ERROR: sidecar missing executor or result_dir_template",
            file=sys.stderr,
        )
        sys.exit(1)

    # --- Resolve task kwargs ---
    try:
        kwargs = tasks.resolve(task_id)
    except Exception as exc:
        print(f"[dispatch] ERROR: tasks.resolve({task_id}) raised: {exc}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(kwargs, dict):
        print(
            f"[dispatch] ERROR: tasks.resolve({task_id}) must return dict, got {type(kwargs).__name__}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        result_dir = _format_result_dir(
            result_dir_template, task_id=task_id, run_id=run_id, kwargs=kwargs
        )
    except KeyError as exc:
        print(f"[dispatch] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    # --- WIP / atomic-promote (preserved from prior dispatcher) ---
    # MapReduce correctness guarantee: write to a temporary work-in-progress
    # directory, then atomically promote files on success. If the task
    # crashes mid-write, partial output stays in _wip_ and never pollutes
    # the final result directory.
    os.makedirs(result_dir, exist_ok=True)
    wip_dir = os.path.join(result_dir, f"_wip_{task_id}")

    if os.path.isdir(wip_dir):
        # On retry, preserve prior failed WIP for forensic inspection.
        stale_target = os.path.join(result_dir, f"_wip_{task_id}_failed_{int(time.time())}")
        try:
            os.rename(wip_dir, stale_target)
            print(f"[dispatch] preserved prior failed WIP at {stale_target}/")
        except OSError as exc:
            print(
                f"[dispatch] WARN: could not preserve stale WIP {wip_dir}: {exc}",
                file=sys.stderr,
            )

    os.makedirs(wip_dir, exist_ok=True)

    env = dict(os.environ)
    env["RESULT_DIR"] = wip_dir
    env["HPC_RESULT_DIR"] = wip_dir
    env["HPC_TASK_ID"] = str(task_id)
    env["HPC_RUN_ID"] = run_id
    for key, value in kwargs.items():
        s = str(value)
        env[key.upper()] = s
        env[f"HPC_KW_{key.upper()}"] = s

    print(f"[dispatch] task_id={task_id} run_id={run_id} result_dir={result_dir}")
    print(f"[dispatch] cmd={executor}")

    result = subprocess.run(executor, shell=True, env=env)

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
