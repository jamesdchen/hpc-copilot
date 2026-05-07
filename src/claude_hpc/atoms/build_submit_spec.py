"""``build-submit-spec`` primitive — assemble + validate a submit-flow spec.

Replaces the 200 lines of "set this field, set that field" prose in
``/submit-hpc`` Step 6d. Takes resolved interview outputs (executor,
cluster, profile, ...) and emits a validated ``submit_flow.input.json``
dict ready to pass straight into ``submit-flow``.

The agent's remaining job collapses to:

1. Run the interview / pick executor / score plan (judgment).
2. Call ``build-submit-spec`` with the resolved values.
3. Write the returned dict to a temp file.
4. Invoke ``submit-flow --spec <file>``.

Without this primitive the agent had to remember every job_env key,
the canonical script paths, the per-runtime additions (HPC_RUNTIME=uv,
HPC_CAMPAIGN_ID), and the layered defaults — easy to forget one and
ship a partly-broken spec.
"""

from __future__ import annotations

import json
from importlib.resources import files as _resource_files
from typing import Any

from claude_hpc import errors
from claude_hpc._internal._primitive import primitive
from claude_hpc._schema_models.build_submit_spec import BuildSubmitSpecInput
from claude_hpc.infra.remote import validate_ssh_target

# Canonical cluster-side template paths. The local-side rsync ships the
# generic templates under ``.hpc/templates/`` (deploy_runtime puts them
# there); the script field on the submit_flow spec is just the relative
# path the qsub/sbatch will execute on the cluster.
_DEFAULT_SCRIPTS: dict[tuple[str, bool], str] = {
    ("sge_remote", False): ".hpc/templates/cpu_array.sh",
    ("sge_remote", True): ".hpc/templates/gpu_array.sh",
    ("slurm", False): ".hpc/templates/cpu_array.slurm",
    ("slurm", True): ".hpc/templates/gpu_array.slurm",
}

# Job-env keys the cluster-side dispatcher / template ALWAYS need. The
# slash-command prose used to enumerate these by hand; we synthesize
# them here from the resolved arguments. Anything else the caller wants
# to forward (custom dataset paths, debug flags, ...) goes in the
# ``extra_env`` kwarg.
_DEFAULT_EXECUTOR_CMD = "python3 .hpc/_hpc_dispatch.py"


@primitive(
    name="build-submit-spec",
    verb="scaffold",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    cli="hpc-mapreduce build-submit-spec --spec <path>",
    agent_facing=True,
)
def build_submit_spec(*, spec: BuildSubmitSpecInput) -> dict[str, Any]:
    """Build + validate a ``submit_flow.input.json`` spec dict.

    The wire-validated ``spec`` (a :class:`BuildSubmitSpecInput`)
    carries every field this atom needs; the body destructures it
    into typed locals at the top so the rest reads unchanged.

    Parameters
    ----------
    profile, cluster, ssh_target, remote_path, run_id, total_tasks,
    backend:
        Required identity fields that flow straight through to the
        spec. ``backend`` is one of ``sge_remote`` / ``slurm``.
    cmd_sha:
        SHA-256 of the materialized task list, computed by
        :func:`compute_cmd_sha`. Stamped into ``job_env["HPC_CMD_SHA"]``
        so the cluster-side dispatcher can verify it's running the
        right tasks.py.
    is_gpu:
        Picks ``gpu_array.{sh,slurm}`` over ``cpu_array.{sh,slurm}`` for
        the default *script*. Ignored when *script* is supplied
        explicitly.
    job_name:
        Defaults to *profile* when unset.
    script:
        Cluster-side path to the job script. Defaults to the canonical
        per-(backend, is_gpu) template.
    modules, conda_source, conda_env:
        Cluster env-setup fields, threaded through to the template
        preamble via ``$MODULES`` / ``$CONDA_SOURCE`` / ``$CONDA_ENV``.
        Empty strings are fine (the preamble's defaults take over).
    runtime:
        ``"uv"`` to fire ``uv sync`` before dispatch (sets
        ``HPC_RUNTIME=uv``); any other value or None is omitted.
    campaign_id:
        Closed-loop campaign tag. Stamped on the run sidecar AND
        forwarded to the cluster as ``HPC_CAMPAIGN_ID`` so the user's
        tasks.py can read it at module-load.
    canary, partial_ok, skip_preflight:
        Boolean knobs threaded through to the spec verbatim.
        ``skip_preflight`` defaults to True because Step 6b in the
        slash command runs the preflight gate immediately before this
        spec is built; the duplicate ssh probe is wasteful.
    pass_env_keys:
        SGE-only — which job_env keys to ``qsub -v``. None = forward
        everything in job_env. SLURM forwards everything via
        ``--export ALL,...`` regardless.
    rsync_excludes, slurm_account, slurm_cluster:
        Optional spec passthroughs.
    extra_env:
        Additional job_env keys merged on top of the framework
        defaults. Caller-supplied values WIN over framework defaults
        on key collision (so a caller can override e.g. ``EXECUTOR``
        for a custom dispatcher).

    Returns
    -------
    A dict matching ``schemas/submit_flow.input.json``, validated
    before return. Pass it straight to
    :func:`claude_hpc.flows.submit_flow.submit_flow` or write it to a
    JSON file and call ``hpc-mapreduce submit-flow --spec <file>``.

    Raises
    ------
    :class:`errors.SpecInvalid`
        Any required field is empty / malformed (ssh_target shape,
        unknown backend, total_tasks < 1) OR the assembled spec fails
        schema validation.
    """
    profile = spec.profile
    cluster = spec.cluster
    ssh_target = spec.ssh_target
    remote_path = spec.remote_path
    run_id = spec.run_id
    cmd_sha = spec.cmd_sha
    total_tasks = spec.total_tasks
    backend = spec.backend
    is_gpu = bool(spec.is_gpu)
    job_name = spec.job_name
    script = spec.script
    modules = spec.modules or ""
    conda_source = spec.conda_source or ""
    conda_env = spec.conda_env or ""
    runtime = spec.runtime
    campaign_id = spec.campaign_id or ""
    canary = bool(spec.canary) if spec.canary is not None else True
    partial_ok = bool(spec.partial_ok) if spec.partial_ok is not None else False
    skip_preflight = bool(spec.skip_preflight) if spec.skip_preflight is not None else True
    pass_env_keys = list(spec.pass_env_keys) if spec.pass_env_keys is not None else None
    rsync_excludes = list(spec.rsync_excludes) if spec.rsync_excludes is not None else None
    slurm_account = spec.slurm_account
    slurm_cluster = spec.slurm_cluster
    extra_env = dict(spec.extra_env) if spec.extra_env is not None else None

    try:
        validate_ssh_target(ssh_target)
    except ValueError as exc:
        raise errors.SpecInvalid(str(exc)) from exc

    job_name = job_name or profile
    if script is None:
        script = _DEFAULT_SCRIPTS[(backend, bool(is_gpu))]

    # Framework-default job_env. Caller's extra_env wins via dict merge
    # order (spread caller last).
    job_env: dict[str, str] = {
        "EXECUTOR": _DEFAULT_EXECUTOR_CMD,
        "HPC_RUN_ID": run_id,
        "HPC_CMD_SHA": cmd_sha,
        "HPC_TASK_COUNT": str(int(total_tasks)),
        "REPO_DIR": remote_path,
        "MODULES": modules,
        "CONDA_SOURCE": conda_source,
        "CONDA_ENV": conda_env,
    }
    if runtime == "uv":
        job_env["HPC_RUNTIME"] = "uv"
    if campaign_id:
        job_env["HPC_CAMPAIGN_ID"] = campaign_id
    if extra_env:
        job_env.update({str(k): str(v) for k, v in extra_env.items()})

    out: dict[str, Any] = {
        "profile": profile,
        "cluster": cluster,
        "ssh_target": ssh_target,
        "remote_path": remote_path,
        "run_id": run_id,
        "total_tasks": int(total_tasks),
        "backend": backend,
        "job_name": job_name,
        "script": script,
        "job_env": job_env,
        "canary": bool(canary),
        "partial_ok": bool(partial_ok),
        "skip_preflight": bool(skip_preflight),
    }
    if pass_env_keys is not None:
        out["pass_env_keys"] = list(pass_env_keys)
    if rsync_excludes is not None:
        out["rsync_excludes"] = list(rsync_excludes)
    if slurm_account is not None:
        out["slurm_account"] = slurm_account
    if slurm_cluster is not None:
        out["slurm_cluster"] = slurm_cluster
    if campaign_id:
        out["campaign_id"] = campaign_id
    if runtime is not None:
        out["runtime"] = runtime

    _validate(out)
    return out


def _validate(spec: dict[str, Any]) -> None:
    """Schema-validate *spec*. Raises :class:`errors.SpecInvalid` on miss.

    Inline rather than going through agent_cli's helper so the primitive
    works headless (a non-Claude-Code orchestrator wouldn't import the
    CLI module).
    """
    try:
        import jsonschema  # type: ignore[import-untyped]
    except ImportError:
        return  # defence-in-depth; primitive callers still upstream-validate
    try:
        schema_text = (_resource_files("claude_hpc.schemas") / "submit_flow.input.json").read_text()
    except (FileNotFoundError, ModuleNotFoundError):
        return
    schema = json.loads(schema_text)
    from claude_hpc._internal._schema import validate as _validate

    try:
        _validate(spec, schema)
    except jsonschema.ValidationError as exc:
        path = "/".join(str(p) for p in exc.absolute_path) or "<root>"
        raise errors.SpecInvalid(
            f"build_submit_spec produced invalid spec at {path}: {exc.message}"
        ) from exc
