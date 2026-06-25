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

# Cluster-side path to the single multi-rank (MPI) template per backend (#293).
# An ``mpi`` block on the spec selects this over the cpu/gpu array script
# regardless of ``is_gpu`` — a multi-rank solve is one unit of work, not a
# fan-out. deploy_runtime ships ``.hpc/templates/mpi.{sh,slurm,pbs}``.
_MPI_SCRIPTS: dict[str, str] = {
    "sge": ".hpc/templates/mpi.sh",
    "slurm": ".hpc/templates/mpi.slurm",
    "pbspro": ".hpc/templates/mpi.pbs",
    "torque": ".hpc/templates/mpi.pbs",
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
        # #292: the bare-script / $VAR guards resolve the EXECUTOR's script
        # path and load .hpc/tasks.py RELATIVE to the experiment dir, not the
        # caller's CWD. ``experiment_dir_arg`` injects ``--experiment-dir``
        # (default cwd) so a worker whose CWD isn't the experiment dir can
        # still point the guards at the real tree. The composite
        # ``resolve-submit-inputs`` threads its own experiment_dir through.
        experiment_dir_arg=True,
    ),
    agent_facing=True,
)
def build_submit_spec(
    experiment_dir: Path | None = None, *, spec: BuildSubmitSpecInput
) -> dict[str, Any]:
    """Build + validate a ``submit_flow.input.json`` spec dict.

    The wire-validated ``spec`` (a :class:`BuildSubmitSpecInput`)
    carries every field this atom needs; the body destructures it
    into typed locals at the top so the rest reads unchanged.

    ``experiment_dir`` (optional) is the local experiment tree the
    EXECUTOR's script path and ``.hpc/tasks.py`` are resolved against
    by the defensive guards (#292). When None the guards fall back to
    the process CWD — correct for a standalone invocation run from the
    experiment dir, but NOT for a worker whose CWD differs, which is why
    ``resolve-submit-inputs`` threads its real experiment_dir through.

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
    canary, partial_ok:
        Boolean knobs threaded through to the spec verbatim.
        (``skip_preflight`` was removed in #275 — the preflight skip is
        operator-only now via ``HPC_AGENT_SKIP_PREFLIGHT``, never a built
        spec field; an agent could otherwise silence the uv runtime probe.)
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
    # Env-activation resolved as ONE coherent unit (#281), not three
    # independent strings the caller threads. ``resolve_activation`` back-fills
    # ``conda_source`` from clusters.yaml when a ``conda_env`` is selected but
    # the source was dropped — the 2026-06-05 Hoffman2 incident, where the
    # agent lost ``conda_source`` between ``clusters describe`` and here and
    # the preamble then crashed every task at ``conda: command not found``. The
    # resulting ``Activation`` enforces the coherence invariant at construction:
    # the incoherent partial state (conda_env set, no source AND no
    # conda-loading module) is unrepresentable — it raises ``SpecInvalid`` at
    # this boundary instead of sailing through to a doomed qsub. The old inline
    # all-empty + per-pair guards now live inside ``Activation.__post_init__``.
    from hpc_agent.infra.clusters import load_clusters_config, resolve_activation

    _activation = resolve_activation(
        cluster_cfg=load_clusters_config().get(cluster) or {},
        modules=spec.modules,
        conda_source=spec.conda_source,
        conda_env=spec.conda_env,
    )
    modules = _activation.modules
    conda_source = _activation.conda_source
    conda_env = _activation.conda_env
    runtime = spec.runtime
    campaign_id = spec.campaign_id or ""
    canary = bool(spec.canary) if spec.canary is not None else True
    partial_ok = bool(spec.partial_ok) if spec.partial_ok is not None else False
    # #275: ``skip_preflight`` is no longer emitted onto the submit_flow spec —
    # it was an agent-settable bypass that silenced the uv runtime probe. The
    # preflight skip is operator-only now (``HPC_AGENT_SKIP_PREFLIGHT=1``).
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
    import yaml

    from hpc_agent.infra.clusters import load_clusters_config
    from hpc_agent.infra.ssh_validation import validate_remote_path_under_scratch

    try:
        cluster_scratch = (load_clusters_config().get(cluster) or {}).get("scratch") or ""
    except (OSError, ValueError, yaml.YAMLError):
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
        _check_register_run_executor(extra_env["EXECUTOR"], base_dir=experiment_dir)
        # Sibling guard: a python_module entry must dispatch through
        # ``run-module``, never as a bare ``<module>:<function>`` token — the
        # latter is exec'd as a shell command and exits 127 (the ridge_imp
        # incident). Catches a hand-rolled spec / stale divergent-build sidecar.
        _check_bare_module_executor(extra_env["EXECUTOR"])

    job_name = job_name or profile
    if script is None:
        # #293: an mpi block routes to the single multi-rank template (one unit
        # of work), independent of is_gpu; otherwise the cpu/gpu array script.
        if spec.mpi is not None:
            script = _MPI_SCRIPTS[backend]
        else:
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
    if spec.mpi is not None:
        # #293: the mpi template reads these to fold the launcher + rank count
        # into $EXECUTOR. The scheduler-side allocation (ntasks/select/-pe) is
        # the resource_flags' job; these tell the in-job launcher what to spawn.
        job_env["HPC_MPI_RANKS"] = str(int(spec.mpi.ranks))
        job_env["HPC_MPI_LAUNCHER"] = spec.mpi.launcher
        job_env["HPC_MPI_THREADS_PER_RANK"] = str(int(spec.mpi.threads_per_rank))
    if spec.walltime_sec:
        # #294: surface the walltime to the cluster preamble so it can stamp
        # HPC_WALLTIME_END_EPOCH (job start + walltime) for checkpoint-aware
        # executors — should_checkpoint(strategy="walltime_margin") / run_iterations
        # then checkpoint with margin to spare before the scheduler's walltime kill.
        job_env["HPC_WALLTIME_SEC"] = str(int(spec.walltime_sec))
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

    # #292 Bug B: cross-check the effective EXECUTOR's ``$VAR`` references
    # against the vars the cluster-side dispatcher will actually export. The
    # dispatcher exports ``$<NAME>`` / ``$HPC_KW_<NAME>`` ONLY for keys
    # ``tasks.resolve(i)`` returns; a reference to anything it never sets
    # expands to EMPTY and the command fails downstream (the empirical
    # ``--samples $SAMPLES`` where ``samples`` isn't a swept axis → argparse
    # 'expected one argument'). Refuses at build time so the broken EXECUTOR
    # never reaches the canary. No-ops unless the kwarg set can be positively
    # established from ``experiment_dir/.hpc/tasks.py``, so an unknowable set
    # can never trigger a false refusal.
    # Only resolve the kwarg set — which imports ``.hpc/tasks.py`` — when the
    # effective EXECUTOR actually references a ``$VAR`` worth checking. The
    # default dispatcher command has none, so the common path (and the
    # resolve-submit-inputs composite, which already imported tasks.py for
    # cmd_sha) pays no second user-code import.
    _effective_executor = job_env.get("EXECUTOR", "")
    # str.format {placeholder} leakage is a pure-string check — cheap, no
    # tasks.py import — so run it unconditionally (the default dispatcher
    # command has no braces, so the common path no-ops).
    _check_executor_format_placeholders(_effective_executor)
    if "$" in _effective_executor:
        _check_executor_var_references(
            _effective_executor,
            job_env_keys=set(job_env),
            kwargs_keys=_resolve_kwargs_keys(experiment_dir),
        )

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
    }
    if spec.result_dir_template is not None:
        out["result_dir_template"] = spec.result_dir_template
    resources: dict[str, Any] = {
        k: v
        for k, v in (
            ("walltime_sec", spec.walltime_sec),
            ("mem_mb", spec.mem_mb),
            ("cpus", spec.cpus),
        )
        if v is not None
    }
    if spec.mpi is not None:
        # #293: emit the mpi block onto resources so the backend's resource_flags
        # sizes the job from ranks/topology. model_dump drops the null optionals,
        # keeping the spec minimal.
        resources["mpi"] = spec.mpi.model_dump(exclude_none=True)
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


def _check_register_run_executor(executor: str, *, base_dir: Path | None = None) -> None:
    """Raise :class:`errors.SpecInvalid` if *executor* is a bare-script invocation
    of a ``@register_run``-decorated file.

    Fires on any ``python[3] <file>.py [...]`` shape against a
    ``register_run``-decorated file — including the with-trailing-args form
    (``python executors/foo.py --samples 100000 --seed $SEED``) that the
    pre-0.10.11 strict ``len(parts) == 2`` check let slip through. Trailing
    args are not the safe path — they are the *exact* smoking gun for an
    agent that forgot the canonical ``python -c "..."`` form and shell-
    templated kwargs into argv instead, which the cluster-side dispatcher
    drops on the floor (it routes task kwargs via ``HPC_KW_<NAME>`` env vars,
    not argv). Anything with a flag *before* the script (``python -c "..."``,
    ``python -m pkg``, ``python -O file.py``) short-circuits at the
    ``script.endswith(".py")`` check — those forms are presumed correct.

    *base_dir* (#292 Bug A): the experiment tree the (relative) script path is
    resolved against. The pre-#292 code did ``Path(script).is_file()`` — a
    CWD-relative probe that returned False (and silently passed the guard)
    whenever ``build_submit_spec`` ran in a worker whose CWD wasn't the
    experiment dir, exactly the contract the 0.10.11 CHANGELOG asserts holds.
    When *base_dir* is given, a relative script resolves against it; when None,
    the old CWD-relative behaviour is preserved (correct for an invocation run
    from the experiment dir).
    """
    try:
        parts = shlex.split(executor)
    except ValueError:
        return  # unparseable shell — leave it to the cluster to surface
    if len(parts) < 2:
        return
    interp, script, *_trailing = parts
    if not _BARE_SCRIPT_RE.match(interp):
        return
    if not script.endswith(".py"):
        return
    local_path = Path(script)
    if base_dir is not None and not local_path.is_absolute():
        local_path = Path(base_dir) / local_path
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


# Matches a lone ``<dotted.module>:<function>`` token — the shape a divergent
# build (or a hand-rolled spec) stamps for a python_module entry when it skips
# the run-module dispatch. The module side is a dotted Python identifier path
# and the function side a single identifier, so a Windows drive path (``C:\x``)
# or a URL (``http://``) can't match: those carry a backslash/slash the class
# excludes.
_BARE_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")


def _check_bare_module_executor(executor: str) -> None:
    """Raise :class:`errors.SpecInvalid` if *executor* is a bare ``module:function``.

    A ``python_module`` entry point must dispatch via
    ``python3 -m hpc_agent.executor_cli run-module <module>:<function>`` — never
    the bare ``<module>:<function>`` token alone. The bare form reaches the
    cluster as the per-task command, is exec'd as a shell command, and exits 127
    (command not found): the ridge_imp incident, where a divergent local build
    materialized ``hpc_wrappers.ridge_imp:ridge_imp`` into the sidecar's
    ``executor``. The interview's python_module branch emits the correct
    ``run-module`` form (:func:`wrap_entry_point.python_module_executor_cmd`);
    this guard catches a hand-rolled spec or a stale/divergent-build sidecar.
    """
    try:
        parts = shlex.split(executor)
    except ValueError:
        return  # unparseable shell — leave it to the cluster to surface
    if len(parts) != 1 or not _BARE_MODULE_RE.match(parts[0]):
        return
    raise errors.SpecInvalid(
        f"EXECUTOR is the bare module:function form {executor!r}, which is not a "
        "runnable command — exec'd on the cluster it exits 127 (command not "
        "found). A python_module entry point must dispatch through the deployed "
        "executor_cli:\n"
        f"  python3 -m hpc_agent.executor_cli run-module {executor}\n"
        "The interview's python_module path generates this automatically; if "
        "you're seeing this you're hand-rolling the spec or carrying a stale "
        "sidecar from a divergent build. Re-run the interview (`/submit-hpc`)."
    )


# --- #292 Bug B: EXECUTOR $VAR ↔ exported-env cross-check -------------------
#
# Vars the cluster-side dispatcher / array template inject per-task that are
# NOT carried in the built ``job_env`` (so they wouldn't show up in
# ``job_env.keys()``): the per-task result dir and the task/run identity. A
# ``$RESULT_DIR`` / ``$TASK_ID`` reference is legitimate and must not be
# flagged. Everything else the framework forwards rides ``job_env`` itself.
_FRAMEWORK_INJECTED_VARS: frozenset[str] = frozenset(
    {"RESULT_DIR", "HPC_RESULT_DIR", "TASK_ID", "HPC_TASK_ID", "RUN_ID", "HPC_RUN_ID"}
)

# Common cluster shell vars an EXECUTOR may legitimately inherit from the job
# environment (the user's ``--data $SCRATCH/...`` etc.). The dispatcher's own
# ``_warn_unset_kwarg_refs`` deliberately stays in the unambiguous ``HPC_KW_``
# namespace because a bare ``$SAMPLES`` "can't be reliably told apart from a
# genuine env var"; the build-time refuse resolves that ambiguity with an
# explicit allowlist (exact names + scheduler/runtime prefixes) so a real
# inherited var is never mistaken for an unset-kwarg typo.
_INHERITED_SHELL_VARS: frozenset[str] = frozenset(
    {
        "HOME", "PATH", "USER", "LOGNAME", "SHELL", "PWD", "OLDPWD", "SHLVL",
        "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "TERM", "HOSTNAME", "HOST",
        "SCRATCH", "WORK", "PROJECT", "GROUP", "LD_LIBRARY_PATH", "LIBRARY_PATH",
        "PYTHONPATH", "MANPATH", "CPATH", "CUDA_VISIBLE_DEVICES", "NSLOTS",
        "JOB_ID", "JOB_NAME", "NHOSTS", "NQUEUES", "REPO_DIR",
    }
)  # fmt: skip
_INHERITED_SHELL_PREFIXES: tuple[str, ...] = (
    "SLURM_", "SGE_", "PBS_", "OMPI_", "PMI_", "PMIX_", "MPI_", "OMP_",
    "CUDA_", "NCCL_", "I_MPI_", "HPC_AGENT_", "HPC_SERVICE_",
)  # fmt: skip

# ``$NAME`` or ``${NAME}`` / ``${NAME:-default}``. The braced form keeps any
# trailing modifier so a default-providing reference (``:-``/``-``/``:=``/``=``)
# can be recognised as safe (it never expands to empty on an unset var).
_VAR_REF_RE = re.compile(
    r"\$\{(?P<bname>[A-Za-z_][A-Za-z0-9_]*)(?P<bmod>[^}]*)\}"
    r"|\$(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
)
_DEFAULT_MOD_RE = re.compile(r"^:?[-=]")


def _is_inherited_shell_var(name: str) -> bool:
    """True when *name* is a cluster shell var an EXECUTOR may legitimately use."""
    return name in _INHERITED_SHELL_VARS or name.startswith(_INHERITED_SHELL_PREFIXES)


def _iter_var_refs(executor: str):
    """Yield ``(var_name, is_defaulted)`` for every ``$VAR`` ref in *executor*.

    ``is_defaulted`` is True for the ``${VAR:-x}`` / ``${VAR-x}`` (and ``:=``)
    fallback forms, which are safe even when ``VAR`` is unset and so are never
    flagged.
    """
    for m in _VAR_REF_RE.finditer(executor):
        if m.group("name") is not None:
            yield m.group("name"), False
        else:
            yield m.group("bname"), bool(_DEFAULT_MOD_RE.match(m.group("bmod") or ""))


def _resolve_kwargs_keys(experiment_dir: Path | None) -> set[str] | None:
    """Lowercased per-task kwarg names from ``<experiment_dir>/.hpc/tasks.py``.

    Returns None when the kwarg set can't be *positively* established — no
    experiment_dir, no tasks.py, an import/resolve error, or a zero-task
    sweep. The var-reference check skips entirely on None, so an unknowable
    kwarg set can never produce a false refusal. Best-effort: importing the
    user's tasks.py is a read the framework already does to compute cmd_sha
    (``compute_cmd_sha(load_tasks_module(...))``), so this introduces no new
    class of side effect; any failure degrades to "skip the check".
    """
    if experiment_dir is None:
        return None
    tasks_py = Path(experiment_dir) / ".hpc" / "tasks.py"
    if not tasks_py.is_file():
        return None
    try:
        from hpc_agent import load_tasks_module

        mod = load_tasks_module(tasks_py)
        if int(mod.total()) < 1:
            return None
        kwargs = mod.resolve(0)
        if not isinstance(kwargs, dict):
            return None
        return {str(k).lower() for k in kwargs}
    except Exception:  # noqa: BLE001 — any failure → degrade to "skip"
        return None


def _check_executor_var_references(
    executor: str, *, job_env_keys: set[str], kwargs_keys: set[str] | None
) -> None:
    """Refuse an EXECUTOR that references a ``$VAR`` the dispatcher never exports.

    Covered references (never flagged): a key already in *job_env* (forwarded
    to the job env verbatim), a framework-injected identity/result var, an
    inherited cluster shell var, a ``:-``-defaulted reference, and — the point
    of the check — a real task kwarg, exported by the dispatcher as both bare
    ``$<NAME>`` and ``$HPC_KW_<NAME>``. Anything else (the empirical
    ``$SAMPLES`` for a ``samples`` that isn't a swept axis) is an unset-expands-
    to-empty bug; raise :class:`errors.SpecInvalid` with the two resolutions.

    No-ops when *kwargs_keys* is None (the kwarg set couldn't be established) —
    the conservative posture that only refuses on a *provable* miss.
    """
    # A wrong-case reference to a REAL kwarg ($seed for kwarg seed) is its own
    # provable miss — the dispatcher exports the bare/namespaced form uppercased,
    # so the lowercase spelling expands to empty. Caught here so the build path
    # surfaces it alongside the unset-var check below.
    _check_executor_kwarg_casing(executor, kwargs_keys=kwargs_keys)
    if kwargs_keys is None or "$" not in executor:
        return
    covered = _FRAMEWORK_INJECTED_VARS | set(job_env_keys)
    for ref, defaulted in _iter_var_refs(executor):
        if defaulted or ref in covered or _is_inherited_shell_var(ref):
            continue
        kwarg = ref[len("HPC_KW_") :].lower() if ref.startswith("HPC_KW_") else ref.lower()
        if kwarg in kwargs_keys:
            continue
        raise errors.SpecInvalid(
            f"EXECUTOR references ${ref} but no {kwarg!r} kwarg is exported and it "
            "is not a framework or inherited cluster variable. The cluster-side "
            "dispatcher exports a task kwarg as $<NAME> / $HPC_KW_<NAME> only for "
            f"keys tasks.resolve(i) returns (here: {sorted(kwargs_keys)}). A "
            f"reference the dispatcher never sets expands to EMPTY and the command "
            "fails downstream (e.g. argparse 'expected one argument'). Resolve by "
            "either:\n"
            f"  • adding {kwarg!r} to a homogeneous_axes / fixed_params block so "
            "tasks.resolve() returns it (then it's exported), or\n"
            f"  • dropping the ${ref} reference from the EXECUTOR command."
        )


# --- str.format {placeholder} leakage into the EXECUTOR --------------------
#
# The cluster-side dispatcher str.format()s ONLY result_dir_template (with
# run_id / task_id / swept kwargs); it runs the EXECUTOR through the shell
# verbatim (``subprocess.Popen(executor, shell=True)``). A ``{run_id}`` /
# ``{seed}`` token in the EXECUTOR therefore never expands — it reaches the
# program LITERALLY (the empirical 2026-06-06 demo:
# ``--output-file results/{run_id}/seed_{seed}/metrics.json`` would write under
# a directory named ``{run_id}``). The per-task output dir is ``$RESULT_DIR``;
# the {placeholders} belong in result_dir_template.
#
# Negative lookbehind on ``$`` so shell parameter expansion ``${VAR}`` is not
# mistaken for a format placeholder. Empty ``{}`` (``find -exec``), comma lists
# (``{a,b}``) and numeric ranges (``{1..9}``) don't match the named-identifier
# shape, so they're left alone.
_FORMAT_PLACEHOLDER_RE = re.compile(r"(?<!\$)\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _check_executor_format_placeholders(executor: str) -> None:
    """Raise :class:`errors.SpecInvalid` if *executor* carries ``{name}`` tokens.

    Those are result_dir_template syntax; the dispatcher never ``str.format``\\ s
    the EXECUTOR, so the token reaches the program verbatim.
    """
    found = sorted(set(_FORMAT_PLACEHOLDER_RE.findall(executor or "")))
    if not found:
        return
    refs = ", ".join("{" + name + "}" for name in found)
    raise errors.SpecInvalid(
        f"EXECUTOR carries str.format placeholder(s) {refs}, but the cluster-side "
        "dispatcher str.format()s only result_dir_template — it runs the EXECUTOR "
        "through the shell verbatim, so these tokens reach the program LITERALLY "
        "(e.g. output written under a directory named '{run_id}'). Resolve by:\n"
        "  • routing per-task output through $RESULT_DIR (the dispatcher sets it "
        "per task and promotes metrics.json into the result_dir_template dir), "
        'e.g. --output-file "$RESULT_DIR/metrics.json"; and\n'
        "  • moving the {run_id}/{task_id}/{<kwarg>} placeholders into "
        "result_dir_template, where the dispatcher renders them — reference a swept "
        "kwarg in the command itself as $<NAME> / $HPC_KW_<NAME> (uppercase)."
    )


def _check_executor_kwarg_casing(executor: str, *, kwargs_keys: set[str] | None) -> None:
    """Raise :class:`errors.SpecInvalid` for a swept-kwarg ``$ref`` in the wrong case.

    The dispatcher exports each ``tasks.resolve(i)`` kwarg as ``$<KEY.upper()>``
    AND ``$HPC_KW_<KEY.upper()>`` (dispatch.py does ``env[key.upper()]``). A
    lowercase/mixed-case reference to a real kwarg (``$seed`` for the ``seed``
    kwarg) is never set under that spelling and expands to EMPTY — the empirical
    2026-06-06 demo, where the agent "fixed" a correct ``$SEED`` into a broken
    ``$seed``. No-ops when the kwarg set is unknowable (only refuse on a provable
    miss).
    """
    if kwargs_keys is None or "$" not in executor:
        return
    for ref, defaulted in _iter_var_refs(executor):
        if defaulted:
            continue
        if ref.startswith("HPC_KW_"):
            kwarg = ref[len("HPC_KW_") :].lower()
            exported = "HPC_KW_" + kwarg.upper()
        else:
            kwarg = ref.lower()
            exported = kwarg.upper()
        if kwarg in kwargs_keys and ref != exported:
            raise errors.SpecInvalid(
                f"EXECUTOR references ${ref}, but the cluster-side dispatcher exports "
                f"the {kwarg!r} kwarg only as ${exported} / $HPC_KW_{kwarg.upper()} "
                f"(it does env[key.upper()]). The lowercase/mixed-case ${ref} is never "
                "set and expands to EMPTY (the command then fails downstream, e.g. "
                f"argparse 'expected one argument'). Use ${exported} or "
                f"$HPC_KW_{kwarg.upper()}."
            )


def check_per_task_executor(executor: str, *, experiment_dir: Path | None = None) -> None:
    """Boundary guard for the REAL per-task EXECUTOR (the sidecar's ``executor``).

    The cluster dispatcher reads ``sidecar.executor`` and runs it per task, so a
    structurally broken command here fails silently cluster-side. Catches the two
    shapes the ``#162`` dispatcher-self-recursion guard does NOT cover:

    1. str.format ``{placeholder}`` tokens — the dispatcher formats only
       result_dir_template (:func:`_check_executor_format_placeholders`).
    2. a swept-kwarg ``$ref`` in the wrong case
       (:func:`_check_executor_kwarg_casing`).

    Deliberately omits the job_env-aware unset-var check
    (:func:`_check_executor_var_references`): at sidecar-write time the assembled
    job_env (MODULES / CONDA_* / REPO_DIR / ...) isn't known, and the per-task
    command legitimately inherits those at runtime, so flagging them would
    false-positive. ``build-submit-spec`` runs the full check where job_env IS
    known.
    """
    _check_executor_format_placeholders(executor)
    # A bare ``module:function`` here is the ridge_imp exit-127 class: the
    # dispatcher reads THIS field and execs it as a shell command, so a lone
    # dotted-module:function (a hand-rolled / divergent-build sidecar) becomes
    # command-not-found. The interview emits the correct ``run-module`` form and
    # resolve-submit-inputs writes it deterministically; this is defense-in-depth
    # at the field the dispatcher actually consumes.
    _check_bare_module_executor(executor)
    if "$" in (executor or ""):
        _check_executor_kwarg_casing(executor, kwargs_keys=_resolve_kwargs_keys(experiment_dir))


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
