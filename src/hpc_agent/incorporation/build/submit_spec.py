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
import re
import shlex
from importlib.resources import files as _resource_files
from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.actions.build_submit_spec import BuildSubmitSpecInput
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.infra.ssh_validation import validate_ssh_target

# Canonical cluster-side template paths. The local-side rsync ships the
# generic templates under ``.hpc/templates/`` (deploy_runtime puts them
# there); the script field on the submit_flow spec is just the relative
# path the qsub/sbatch will execute on the cluster.
_DEFAULT_SCRIPTS: dict[tuple[str, bool], str] = {
    ("sge", False): ".hpc/templates/cpu_array.sh",
    ("sge", True): ".hpc/templates/gpu_array.sh",
    ("slurm", False): ".hpc/templates/cpu_array.slurm",
    ("slurm", True): ".hpc/templates/gpu_array.slurm",
    # pbspro/torque both render to ``.pbs`` (a cluster is exactly one PBS
    # fork, and deploy_runtime ships only that family's scripts, so the
    # shared ``.pbs`` name never collides on a given cluster).
    ("pbspro", False): ".hpc/templates/cpu_array.pbs",
    ("pbspro", True): ".hpc/templates/gpu_array.pbs",
    ("torque", False): ".hpc/templates/cpu_array.pbs",
    ("torque", True): ".hpc/templates/gpu_array.pbs",
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
    cli=CliShape(
        help=(
            "Assemble + validate a submit_flow.input.json spec from "
            "resolved interview values (profile/cluster/ssh_target/.../"
            "cmd_sha/total_tasks). Emits the spec on stdout. Slash "
            "commands pipe the output straight into 'submit-flow --spec'."
        ),
        spec_arg=True,
        spec_model=BuildSubmitSpecInput,
        schema_ref=SchemaRef(input="build_submit_spec"),
    ),
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
        spec. ``backend`` is one of ``sge`` / ``slurm`` / ``pbspro`` /
        ``torque`` (all resolve to the remote-over-ssh backend).
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
    :func:`hpc_agent.ops.submit_flow.submit_flow` or write it to a
    JSON file and call ``hpc-agent submit-flow --spec <file>``.

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
    # At least ONE env-activation mechanism must be declared. The preamble
    # has no defaults — when all three are empty it skips `module load`,
    # `source $CONDA_SOURCE`, and `conda activate $CONDA_ENV` entirely, and
    # the job runs with whatever python the SSH login shell happened to
    # inherit. That's the empirical failure when clusters.yaml hasn't been
    # onboarded: the cluster runs the wrong (or no) python, the canary
    # crashes, and the bad sidecar poisons later submit dedup. Refuse at
    # the boundary instead of letting it through to qsub.
    if not (modules or conda_source or conda_env):
        raise errors.SpecInvalid(
            "submission has no env-activation declared: modules, conda_source, "
            "and conda_env are all empty. The cluster-side preamble would skip "
            "every env-setup step and run whatever python the SSH login shell "
            "happens to inherit, which usually fails. Populate at least one of "
            "these in clusters.yaml (commonly `conda_source` + `conda_envs`, "
            "or `modules`) and re-run `hpc-agent setup --cluster <name>` to "
            "regenerate the resolved spec."
        )
    runtime = spec.runtime
    campaign_id = spec.campaign_id or ""
    canary = bool(spec.canary) if spec.canary is not None else True
    partial_ok = bool(spec.partial_ok) if spec.partial_ok is not None else False
    skip_preflight = bool(spec.skip_preflight) if spec.skip_preflight is not None else True
    invalidate_on_code_change = (
        bool(spec.invalidate_on_code_change)
        if spec.invalidate_on_code_change is not None
        else False
    )
    pass_env_keys = list(spec.pass_env_keys) if spec.pass_env_keys is not None else None
    rsync_excludes = list(spec.rsync_excludes) if spec.rsync_excludes is not None else None
    slurm_account = spec.slurm_account
    slurm_cluster = spec.slurm_cluster
    extra_env = dict(spec.extra_env) if spec.extra_env is not None else None

    try:
        validate_ssh_target(ssh_target)
    except ValueError as exc:
        raise errors.SpecInvalid(str(exc)) from exc

    # ``remote_path`` becomes ``REPO_DIR`` in the cluster job env; the
    # preamble's ``cd "$REPO_DIR"`` requires an absolute path or it runs
    # from an unpredictable SSH login directory and almost certainly
    # fails. Empirical failure: a half-resolved cluster config produced
    # ``REPO_DIR=monte_carlo_pi-bc3eb1b5``, the canary crashed cluster-
    # side, and the bad sidecar poisoned later submit dedup. Reject the
    # relative path at the submission boundary so it never reaches qsub.
    # (``validate_remote_path`` itself stays permissive — it's also
    # called from raw ``rsync_push`` / ``rsync_pull``, which work fine
    # with relative paths anchored at the SSH login dir.)
    if not remote_path.startswith("/"):
        raise errors.SpecInvalid(
            f"remote_path must be an absolute Unix path (start with '/'): "
            f"{remote_path!r}. A relative remote_path becomes REPO_DIR in the "
            'cluster job env and the preamble\'s `cd "$REPO_DIR"` runs from '
            "an unpredictable SSH login dir. This usually signals that "
            "clusters.yaml hasn't been onboarded yet — run `hpc-agent setup "
            "--cluster <name>` and resubmit."
        )

    # #184: refuse remote_path == cluster scratch root (or shallower). The
    # cluster's scratch is the *parent* dir under which each experiment lives;
    # taking it verbatim made a deploy --delete pre-clean walk every sibling
    # project. Validator no-ops when scratch is undeclared.
    from hpc_agent.infra.clusters import load_clusters_config
    from hpc_agent.infra.ssh_validation import validate_remote_path_under_scratch

    try:
        cluster_scratch = (load_clusters_config().get(cluster) or {}).get("scratch") or ""
    except (OSError, ValueError):
        cluster_scratch = ""
    validate_remote_path_under_scratch(remote_path, cluster_scratch)

    # Defensive preflight: refuse a bare-script EXECUTOR (e.g.
    # ``python3 executors/foo.py``) when ``foo.py`` is actually a
    # ``@register_run``-decorated file. The cluster-side dispatcher passes
    # task kwargs only via ``HPC_KW_<NAME>`` env vars, never argv, so a
    # naive script invocation hits the file's argparse-driven ``__main__``
    # block and exits with "required argument missing" — empirically
    # observed in the 0.10.2 Hoffman2 demo where 100 tasks ran with exit 0
    # but produced no metrics.json (argparse exit 2 silenced downstream).
    # The interview path auto-generates a ``python3 -c "...; _m.compute(_n)"``
    # one-liner for ``register_run`` entry points; if the caller is hand-
    # rolling extra_env or carrying a pre-fix interview, catch it here.
    if extra_env and "EXECUTOR" in extra_env:
        _check_register_run_executor(extra_env["EXECUTOR"])

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
    # Service-dependency passthrough (#231 Tier 1): ship the externally-
    # provisioned address as JSON ``HPC_SERVICE_ENV`` so the cluster-side
    # dispatcher threads each entry into every task's env as
    # ``HPC_SERVICE_<KEY>``. Stamped before extra_env so an explicit
    # caller override still wins.
    if spec.service_env:
        job_env["HPC_SERVICE_ENV"] = json.dumps(
            {str(k): str(v) for k, v in spec.service_env.items()}, sort_keys=True
        )
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
    if spec.result_dir_template is not None:
        out["result_dir_template"] = spec.result_dir_template
    resources = {
        k: v
        for k, v in (
            ("walltime_sec", spec.walltime_sec),
            ("mem_mb", spec.mem_mb),
            ("cpus", spec.cpus),
        )
        if v is not None
    }
    if resources:
        out["resources"] = resources
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
    # Emit only when opted in so the common spec stays byte-identical to
    # the pre-#207 shape (the default param-only dedup needs no flag).
    if invalidate_on_code_change:
        out["invalidate_on_code_change"] = True

    _validate(out)
    return out


# Matches ``python`` / ``python3`` (possibly version-suffixed) followed by
# exactly one positional ``<path>.py`` token — the naive bare-script shape.
# A ``-c`` / ``-m`` / any other flag short-circuits the match: those forms
# are presumed correct (the auto-generated ``python3 -c "..."`` one-liner
# is exactly the path we want to allow through).
_BARE_SCRIPT_RE = re.compile(r"^python[0-9.]*$")


def _check_register_run_executor(executor: str) -> None:
    """Raise :class:`errors.SpecInvalid` if *executor* is a bare-script invocation
    of a ``@register_run``-decorated file.

    Permissive by design: only fires on the exact ``python[3] <file>.py``
    shape. Anything with ``-c`` / ``-m`` / extra flags is presumed correct
    and short-circuits before any filesystem check.
    """
    try:
        parts = shlex.split(executor)
    except ValueError:
        return  # unparseable shell — leave it to the cluster to surface
    if len(parts) != 2:
        return
    interp, script = parts
    if not _BARE_SCRIPT_RE.match(interp):
        return
    if not script.endswith(".py"):
        return
    local_path = Path(script)
    if not local_path.is_file():
        return
    try:
        source = local_path.read_text(encoding="utf-8")
    except OSError:
        return
    # Cheap substring probe — discover.py does the rigorous AST walk, but
    # for a defensive boundary guard the combined presence of both names
    # is a strong-enough signal. A false positive on a comment-only file
    # that mentions both strings is recoverable via the SpecInvalid below.
    if "register_run" not in source or "hpc_agent" not in source:
        return
    raise errors.SpecInvalid(
        f"EXECUTOR is the bare-script form {executor!r}, but {script} is a "
        "@register_run-decorated file. The cluster-side dispatcher passes "
        "task kwargs only via HPC_KW_<NAME> env vars, never argv, so this "
        "invocation will hit the file's argparse __main__ block and fail "
        "with 'required argument missing' (the failure is often silent — "
        "argparse's exit 2 gets eaten and no metrics.json is written).\n"
        "Use the one-liner form instead, e.g.:\n"
        f"  python3 -c \"import runpy as _r; _m = _r.run_path('{script}'); "
        '_n = next(v for v in _m.values() if getattr(v, "_hpc_run", False)); '
        '_m.compute(_n)"\n'
        "The framework's interview path generates this automatically for "
        "register_run entry points — if you're seeing this error, you're "
        "probably constructing the spec by hand or carrying an older "
        "interview from before the auto-generation fix. Re-run the "
        "interview (`hpc-agent setup` / `/submit-hpc`) to regenerate."
    )


def _validate(spec: dict[str, Any]) -> None:
    """Schema-validate *spec*. Raises :class:`errors.SpecInvalid` on miss.

    Inline rather than going through the CLI adapter helper so the
    primitive works headless (a non-Claude-Code orchestrator wouldn't
    import the CLI module).
    """
    try:
        import jsonschema  # type: ignore[import-untyped]
    except ImportError:
        return  # defence-in-depth; primitive callers still upstream-validate
    try:
        schema_text = (_resource_files("hpc_agent.schemas") / "submit_flow.input.json").read_text(
            encoding="utf-8"
        )
    except (FileNotFoundError, ModuleNotFoundError):
        return
    schema = json.loads(schema_text)
    from hpc_agent._kernel.contract.schema import validate as _validate

    try:
        _validate(spec, schema)
    except jsonschema.ValidationError as exc:
        path = "/".join(str(p) for p in exc.absolute_path) or "<root>"
        raise errors.SpecInvalid(
            f"build_submit_spec produced invalid spec at {path}: {exc.message}"
        ) from exc
