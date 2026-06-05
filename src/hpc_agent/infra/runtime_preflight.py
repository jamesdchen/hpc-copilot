"""Cluster-side runtime-availability preflight — the ``command -v uv`` probe.

Extracted from :mod:`hpc_agent.ops.submit_flow` so BOTH the submit pipeline
(``submit-flow``) and the ``check-preflight`` primitive can run the SAME probe
without crossing a subject boundary (#275): ``ops/preflight`` may not import
``ops/submit_flow``, so the one shared implementation lives here in ``infra``
(the sanctioned shared layer). ``submit_flow`` re-exports it under its historic
private name; ``check-preflight`` imports it directly.
"""

from __future__ import annotations

from hpc_agent import errors
from hpc_agent.infra.remote import ssh_run

__all__ = ["runtime_uv_preflight"]


def runtime_uv_preflight(
    ssh_target: str,
    *,
    job_env: dict[str, str],
    skip: bool,
) -> None:
    """When ``HPC_RUNTIME=uv``, verify ``uv`` is on PATH after the cluster
    env is activated — BEFORE the canary qsub (or from ``check-preflight``).

    The job preamble runs ``module load $MODULES``, ``source
    $CONDA_SOURCE``, ``conda activate $CONDA_ENV``, then checks
    ``command -v uv`` (rejecting the run if missing). Reproducing that
    sequence once over SSH at submit time turns "all 100 tasks fail
    with `[template] HPC_RUNTIME=uv but 'uv' not on PATH`" into a single
    `SpecInvalid` at preflight with an actionable remediation.

    Reads activation fields from *job_env* (the dict assembled by
    :func:`build_submit_spec`). Skipped when ``HPC_RUNTIME`` is not
    ``"uv"`` (no other runtime currently triggers a binary-availability
    constraint) or when *skip* is set (operator opted out of the probe).
    """
    if skip or job_env.get("HPC_RUNTIME") != "uv":
        return

    modules = (job_env.get("MODULES") or "").strip()
    conda_source = (job_env.get("CONDA_SOURCE") or "").strip()
    conda_env = (job_env.get("CONDA_ENV") or "").strip()

    parts: list[str] = []
    if modules:
        parts.append(f"module load {modules}")
    if conda_source:
        parts.append(f"source {conda_source}")
    if conda_env:
        parts.append(f"conda activate {conda_env}")
    parts.append("command -v uv")
    cmd = " && ".join(parts)

    probe = ssh_run(cmd, ssh_target=ssh_target)
    if probe.returncode != 0 or not (probe.stdout or "").strip():
        env_hint = (
            f"~/.conda/envs/{conda_env}/bin/pip install uv" if conda_env else "pip install uv"
        )
        raise errors.SpecInvalid(
            f"preflight: runtime=uv but `uv` was not found on PATH after activating "
            f"the cluster env on {ssh_target}. Without it, every task fails "
            f"`[template] HPC_RUNTIME=uv but 'uv' not on PATH`. Install uv into the "
            f"env (e.g. `{env_hint}`) and resubmit, OR drop `runtime: uv` from the "
            f"spec if the repo doesn't actually need uv. "
            f"Activation command attempted: `{cmd}` (exit {probe.returncode}; "
            f"stderr: {(probe.stderr or '').strip()[:200]})."
        )
