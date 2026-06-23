"""``local-reduce`` — run the user's reducer LOCALLY over fetched artifacts.

The pure-API (``requires_ssh = False``) counterpart of
:mod:`hpc_agent.ops.aggregate.cluster_reduce`. A backend with no login node
ships its per-task artifacts back via :meth:`HPCBackend.fetch_results`; this
runs the same reducer-contract command (``docs/reference/reducer-contract.md``)
as a LOCAL subprocess over those fetched files instead of over SSH on the
cluster.

The split that matters: reduction *choice* (numeric weighted-mean vs. a custom
reducer) follows ``aggregate-flow``'s ``mode``; reduction *location* (local vs.
cluster) follows the backend's ``requires_ssh`` capability. The two are
orthogonal, so a pure-API backend is no more locked into the mean than an SSH
one is.

Contract delta for the local case: the reducer finds its inputs under
``$HPC_RESULTS_DIR`` (the dir ``fetch_results`` extracted artifacts into, also
the subprocess cwd) rather than the cluster ``remote_path``. Everything else is
identical — read ``$HPC_RUN_ID``, write one JSON file to
``$HPC_AGGREGATED_OUTPUT``, exit 0. Because the reducer runs on the control
plane, its dependencies must be importable there (the cluster's run env is not
available locally).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent.ops.aggregate._reducer_contract import (
    DEFAULT_OUTPUT_REL,
    format_output_rel,
    parse_reducer_output,
)

__all__ = ["local_reduce"]


def local_reduce(
    *,
    run_id: str,
    results_dir: str | Path,
    aggregate_cmd: str,
    output_path: str | None = None,
    extra_env: dict[str, str] | None = None,
    timeout_sec: int = 1800,
) -> dict[str, Any]:
    """Run *aggregate_cmd* locally over *results_dir*; return its parsed JSON.

    Mirrors :func:`hpc_agent.ops.aggregate.cluster_reduce.cluster_reduce`'s
    return envelope, but executes the reducer as a local subprocess
    (``cwd=results_dir``) instead of over SSH.

    Parameters
    ----------
    run_id:
        Run identifier — exported as ``$HPC_RUN_ID`` for the reducer.
    results_dir:
        Local directory the backend's ``fetch_results`` extracted per-task
        artifacts into. Becomes the subprocess cwd and ``$HPC_RESULTS_DIR``.
    aggregate_cmd:
        Shell command implementing the reducer contract.
    output_path:
        Where the reducer writes its single JSON output, relative to
        *results_dir* (``{run_id}`` substituted). Defaults to
        ``_aggregated/<run_id>.json``. Threaded as ``$HPC_AGGREGATED_OUTPUT``.
    extra_env:
        Additional env vars forwarded to the reducer.
    timeout_sec:
        Reducer subprocess timeout (default 1800s = 30 min).

    Returns
    -------
    ``{ok, run_id, output_path_local, reduced, exit_code, stderr_tail}`` —
    the same shape ``cluster_reduce`` returns, so callers consume both
    reduction paths identically. ``reduced`` is the parsed JSON.

    Raises
    ------
    :class:`errors.SpecInvalid`
        Empty *run_id* or *aggregate_cmd*.
    :class:`errors.RemoteCommandFailed`
        Reducer timed out, exited non-zero, or wrote no/invalid JSON. (Same
        type the SSH path raises so error handling stays transport-neutral,
        even though execution is local.)
    """
    if not run_id:
        raise errors.SpecInvalid("run_id is required")
    if not aggregate_cmd:
        raise errors.SpecInvalid("aggregate_cmd is required for local-reduce")

    results = Path(results_dir)
    output_rel = format_output_rel(output_path or DEFAULT_OUTPUT_REL, run_id=run_id)
    # Anchor the reducer's output under the fetched results dir so it lands
    # beside the artifacts it reduced (and survives for inspection).
    local_output = results / output_rel
    local_output.parent.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["HPC_RUN_ID"] = run_id
    env["HPC_AGGREGATED_OUTPUT"] = str(local_output)
    env["HPC_RESULTS_DIR"] = str(results)
    if extra_env:
        env.update({k: str(v) for k, v in extra_env.items()})

    try:
        proc = subprocess.run(  # noqa: S602 — user's own reducer, their trust domain (same as the cluster path)
            aggregate_cmd,
            shell=True,
            cwd=str(results),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=float(timeout_sec),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise errors.RemoteCommandFailed(
            f"local reducer for run_id={run_id!r} timed out after {timeout_sec}s"
        ) from exc

    stderr_tail = (proc.stderr or "")[-2000:]
    if proc.returncode != 0:
        raise errors.RemoteCommandFailed(
            f"local reducer for run_id={run_id!r} exited {proc.returncode}: "
            f"{stderr_tail.strip()[:500]}"
        )

    reduced = parse_reducer_output(local_output, run_id=run_id)
    return {
        "ok": True,
        "run_id": run_id,
        "output_path_local": str(local_output),
        "reduced": reduced,
        "exit_code": int(proc.returncode),
        "stderr_tail": stderr_tail,
    }
