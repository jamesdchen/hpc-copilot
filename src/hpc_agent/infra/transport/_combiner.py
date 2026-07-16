"""On-cluster combiner / reduce invocations over SSH.

Thin wrappers that run ``.hpc/_hpc_combiner.py`` on the login node — per-wave
combine, its checked ``(ok, stdout, stderr)`` variant, and the cross-wave final
reduce. Each funnels through :func:`hpc_agent.infra.remote.ssh_run`, so they
inherit its bounded-capture discipline; the transfer/deploy engine proper lives
in :mod:`hpc_agent.infra.transport.__init__`.
"""

from __future__ import annotations

import shlex
import subprocess

from hpc_agent.infra.remote import ssh_run

from ._disclose import run_with_stage_heartbeat
from ._shared import _DEFAULT


def run_combiner(
    *,
    ssh_target: str,
    remote_path: str,
    wave: int,
    run_id: str,
    force: bool = False,
    timeout: float | None = _DEFAULT,
    remote_activation: str = "",
) -> subprocess.CompletedProcess[str]:
    """Run the on-cluster combiner on the login node for a specific wave.

    Executes ``.hpc/_hpc_combiner.py`` on the remote host via SSH. The
    combiner accepts both CLI flags (preferred) and ``HPC_WAVE`` /
    ``HPC_RUN_ID`` env vars; we pass both.

    Parameters
    ----------
    ssh_target, remote_path:
        SSH target and remote project root.
    wave:
        Wave number (0-based) to combine.
    run_id:
        Run identifier — locates the per-run sidecar at
        ``.hpc/runs/<run_id>.json`` from which the combiner reads
        ``wave_map`` and ``result_dir_template``.
    force:
        If True, pass ``--force`` so the combiner overwrites any existing
        ``_combiner/wave_N.json`` output.
    timeout:
        Per-call subprocess timeout in seconds, threaded through to
        :func:`ssh_run`. Defaults to :data:`SSH_TIMEOUT_SEC` when omitted.
    """
    force_flag = " --force" if force else ""
    run_id_q = shlex.quote(run_id)
    cmd = (
        f"cd {shlex.quote(remote_path)} && "
        f"{remote_activation}"
        f"HPC_WAVE={wave} HPC_RUN_ID={run_id_q} "
        f"python3 .hpc/_hpc_combiner.py --wave {wave} --run-id {run_id_q}{force_flag}"
    )

    def _do() -> subprocess.CompletedProcess[str]:
        if timeout is _DEFAULT:
            return ssh_run(cmd, ssh_target=ssh_target)
        return ssh_run(cmd, ssh_target=ssh_target, timeout=timeout)

    return run_with_stage_heartbeat(f"combine: wave {wave}", ssh_target, _do)


def run_combiner_checked(
    *,
    ssh_target: str,
    remote_path: str,
    wave: int,
    run_id: str,
    force: bool = False,
    timeout: float | None = _DEFAULT,
    remote_activation: str = "",
) -> tuple[bool, str, str]:
    """Run the combiner and return ``(ok, stdout, stderr)``.

    Thin wrapper around :func:`run_combiner` that collapses
    ``CompletedProcess`` into a simple tuple. ``ok`` is ``True`` iff the
    remote combiner exited with returncode ``0``. A timeout propagates
    as :class:`TimeoutError`, not ``ok=False``.
    """
    if timeout is _DEFAULT:
        result = run_combiner(
            ssh_target=ssh_target,
            remote_path=remote_path,
            wave=wave,
            run_id=run_id,
            force=force,
            remote_activation=remote_activation,
        )
    else:
        result = run_combiner(
            ssh_target=ssh_target,
            remote_path=remote_path,
            wave=wave,
            run_id=run_id,
            force=force,
            timeout=timeout,
            remote_activation=remote_activation,
        )
    return (
        result.returncode == 0,
        result.stdout or "",
        result.stderr or "",
    )


def run_final_reduce(
    *,
    ssh_target: str,
    remote_path: str,
    run_id: str,
    force: bool = False,
    timeout: float | None = _DEFAULT,
    remote_activation: str = "",
) -> subprocess.CompletedProcess[str]:
    """Run the cluster-side FINAL cross-wave reduce on the login node (#254).

    Invokes ``.hpc/_hpc_combiner.py --final --run-id <id>`` over SSH. The
    combiner merges every ``_combiner/wave_*.json`` into a single
    ``_aggregated/<run_id>/metrics_aggregate.json`` on the cluster, so the
    caller pulls one kilobyte-scale file instead of hundreds of wave partials.
    Mirrors :func:`run_combiner` (same activation + timeout contract); pass
    ``force=True`` to overwrite an existing aggregate.
    """
    force_flag = " --force" if force else ""
    run_id_q = shlex.quote(run_id)
    cmd = (
        f"cd {shlex.quote(remote_path)} && "
        f"{remote_activation}"
        f"HPC_RUN_ID={run_id_q} "
        f"python3 .hpc/_hpc_combiner.py --final --run-id {run_id_q}{force_flag}"
    )

    def _do() -> subprocess.CompletedProcess[str]:
        if timeout is _DEFAULT:
            return ssh_run(cmd, ssh_target=ssh_target)
        return ssh_run(cmd, ssh_target=ssh_target, timeout=timeout)

    return run_with_stage_heartbeat("final reduce", ssh_target, _do)
