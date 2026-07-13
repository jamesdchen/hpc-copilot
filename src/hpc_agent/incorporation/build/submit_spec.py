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
from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.actions.build_submit_spec import BuildSubmitSpecInput
from hpc_agent.cli._dispatch import CliShape, SchemaRef

# The ~950-line executor-shape guard library was extracted to the shared
# ``hpc_agent.infra.executor_guard`` substrate. Homing it under ``infra`` is what
# lets ``incorporation`` stop importing from ``ops.submit_flow`` (and ``ops`` stop
# importing back here) — the incorporation↔ops cycle is broken, restoring
# incorporation/README.md's "never consumes from ops/meta" invariant.
# ``build_submit_spec`` below calls these guards by their bare (module-global)
# names, and the test suite pins several of these import paths, so the whole set
# is re-exported at module scope. Module-global re-export is also what keeps the
# ``monkeypatch.setattr(submit_spec, "_resolve_kwargs_keys", ...)`` seam reaching
# ``build_submit_spec``'s call site.
from hpc_agent.infra.executor_guard import (  # noqa: F401 — re-exported for callers + test-pinned paths
    _DEFAULT_EXECUTOR_CMD,
    _check_bare_module_executor,
    _check_bare_script_executor,
    _check_executor_format_placeholders,
    _check_executor_in_deploy_manifest,
    _check_executor_is_dispatcher,
    _check_executor_kwarg_casing,
    _check_executor_var_references,
    _check_register_run_executor,
    _resolve_kwargs_keys,
    _warn_task_interface_blind_executor,
    check_per_task_executor,
)
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
# ``extra_env`` kwarg. The dispatcher command default ``EXECUTOR`` value —
# ``_DEFAULT_EXECUTOR_CMD`` — is re-exported above from
# ``hpc_agent.infra.executor_guard`` (its ``_check_executor_is_dispatcher`` guard
# owns the same constant).


def _campaign_strategy_kw_env(experiment_dir: Path | None, campaign_id: str) -> dict[str, str]:
    """Materialize a campaign manifest's ``strategy.params`` as ``HPC_KW_*`` env.

    A Path-B strategy ``tasks.py`` reads its knobs from
    ``os.environ["HPC_KW_<PARAM>"]`` (the documented convention). The campaign
    manifest stores them under ``strategy.params`` at
    ``<experiment_dir>/.hpc/campaigns/<campaign_id>/manifest.json`` — but
    nothing wired the two together, so a campaign submit enumerated ``tasks.py``
    under default knobs (the ``cmd_sha = e3b0c442…`` empty-list hash → canary
    ``dispatcher_failed``). This is the symmetric missing half of the cluster
    dispatcher's ``resolve()``-kwargs → ``HPC_KW_*`` export (dispatch.py:815),
    for STRATEGY params.

    Returns ``{"HPC_KW_<KEY.upper()>": str(value), ...}`` for each entry in
    ``strategy.params``, stringifying values exactly as the dispatcher does
    (``str(value)``). Returns ``{}`` (clean no-op) when there is no
    experiment_dir, no campaign_id, no manifest, or no strategy params — so a
    non-campaign submit is byte-identical to before.
    """
    if experiment_dir is None or not campaign_id:
        return {}
    try:
        from hpc_agent.meta.campaign.manifest import read_manifest

        manifest = read_manifest(experiment_dir, campaign_id)
    except Exception:  # noqa: BLE001 — a missing/bad manifest must not break a non-campaign-shaped submit
        return {}
    if not isinstance(manifest, dict):
        return {}
    strategy = manifest.get("strategy")
    if not isinstance(strategy, dict):
        return {}
    params = strategy.get("params")
    if not isinstance(params, dict):
        return {}
    return {f"HPC_KW_{str(key).upper()}": str(value) for key, value in params.items()}


def _cross_check_cluster_identity(
    *,
    cluster: str,
    all_clusters: dict[str, Any],
    cluster_cfg: dict[str, Any],
    ssh_target: str,
    backend: str,
) -> None:
    """Cross-check hand-authorable identity fields against clusters.yaml.

    Three proving-run-5 refusals, all firing at submit-time (loud) instead of
    on the cluster (exit-127 / wrong-cluster poll). Each closes a field that is
    caller-authorable but never validated against derivable truth:

    * **finding 20** — an unknown ``cluster`` (a typo) silently degrades every
      cluster-derived field (nfs staging / env activation / array cap) to ``{}``
      and fails only at verify-canary. When clusters.yaml is POPULATED and does
      not carry ``cluster``, refuse with :class:`errors.ClusterUnknown` naming
      the known clusters (mirrors ``plan_throughput``). An EMPTY config
      (``{}`` — an ad-hoc cluster, a fresh install, or an isolated test) has
      nothing to typo against, so it stays a pass-through (unchanged behaviour).
    * **finding 18** — an ``ssh_target`` disagreeing with the cluster's derived
      ``user@host`` runs the job on one host while every cluster-derived
      decision (activation, array cap, scheduler family, run identity) keys on
      ``cluster`` — finding-9's true split-brain root. Refuse the mismatch when
      the entry yields a derivable ssh_target (both ``host`` and ``user`` set).
    * **finding 19** — a ``backend`` (which drives the deploy templates + submit
      grammar) disagreeing with ``clusters.yaml[cluster].scheduler`` (what
      verify / monitor DERIVE) submits under the wrong grammar. Refuse unless
      the entry pins a ``scheduler_profile`` — the sanctioned override, where
      ``build_remote_backend`` enforces ``backend == profile.family`` instead
      (``infra/backends/remote_factory.py``).
    """
    # finding 20 — an unknown cluster, but only when there IS a populated config
    # to have typo'd against; an empty {} is the ad-hoc / fresh / test case.
    if all_clusters and not cluster_cfg:
        raise errors.ClusterUnknown(
            f"cluster {cluster!r} is not defined in clusters.yaml; known clusters: "
            f"{sorted(all_clusters)}. A typo'd cluster silently degrades every "
            "cluster-derived field (nfs staging, env activation, array cap) to "
            "empty and fails only later at verify-canary. Fix the cluster name, "
            "or run `hpc-agent clusters list` to see what is configured."
        )
    if not cluster_cfg:
        return  # empty / ad-hoc config: nothing derivable to cross-check against.

    # finding 18 — ssh_target must equal the cluster's derived user@host. Use
    # ClusterConfig.ssh_target as the single owner of that derivation; a thin
    # entry that can't validate (or lacks host/user) yields None → skip.
    from hpc_agent.infra.clusters import ClusterConfig

    try:
        derived_ssh = ClusterConfig.model_validate(cluster_cfg).ssh_target
    except Exception:  # noqa: BLE001 — a malformed entry is another guard's concern
        derived_ssh = None
    if derived_ssh and ssh_target.strip() != derived_ssh:
        raise errors.SpecInvalid(
            f"ssh_target {ssh_target!r} disagrees with cluster {cluster!r}'s "
            f"derived target {derived_ssh!r} (user@host from clusters.yaml). The "
            "job would run on the ssh_target host while every cluster-derived "
            "decision (env activation, array cap, scheduler family, run identity) "
            f"keys on cluster={cluster!r} — the finding-9 split-brain, where a "
            "retarget set cluster and ssh_target to different clusters and the "
            "run executed on one while status polled the other. Set ssh_target to "
            f"{derived_ssh!r}, or correct the cluster."
        )

    # finding 19 — backend must match the cluster's scheduler family. A pinned
    # scheduler_profile is the sanctioned override (remote_factory then enforces
    # backend == profile.family), so skip the check when one is present.
    if not cluster_cfg.get("scheduler_profile"):
        scheduler = str(cluster_cfg.get("scheduler") or "").strip().lower()
        if scheduler and backend.strip().lower() != scheduler:
            raise errors.SpecInvalid(
                f"backend {backend!r} disagrees with cluster {cluster!r}'s "
                f"scheduler {scheduler!r} (clusters.yaml). backend drives the "
                "deploy templates + submit grammar, while verify/monitor DERIVE "
                "the scheduler from clusters.yaml[cluster] — a mismatch deploys "
                "one family's scripts and submits with another's flags. Set "
                f"backend to {scheduler!r}, or pin a scheduler_profile on the "
                "cluster entry to override the family."
            )


@primitive(
    name="build-submit-spec",
    verb="scaffold",
    side_effects=[],
    error_codes=[errors.SpecInvalid, errors.ClusterUnknown],
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
        schema validation OR (proving-run-5 findings 18/19) ``ssh_target``
        disagrees with the cluster's derived ``user@host`` or ``backend``
        disagrees with the cluster's ``scheduler``.
    :class:`errors.ClusterUnknown`
        (proving-run-5 finding 20) ``cluster`` is absent from a populated
        clusters.yaml — a typo that would otherwise degrade every
        cluster-derived field to empty and fail only at verify-canary.
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

    _all_clusters = load_clusters_config()
    _cluster_cfg = _all_clusters.get(cluster) or {}
    # proving-run-5 findings 18/19/20: cross-check the hand-authorable identity
    # fields (cluster / ssh_target / backend) against clusters.yaml at
    # submit-time, so a typo or a split-brain retarget refuses LOUDLY here
    # instead of failing late on the cluster (exit-127 / wrong-cluster poll).
    _cross_check_cluster_identity(
        cluster=cluster,
        all_clusters=_all_clusters,
        cluster_cfg=_cluster_cfg,
        ssh_target=ssh_target,
        backend=backend,
    )
    # Minor fix: a spec built directly (not via the worker) may omit all three
    # env-activation fields even though clusters.yaml carries them for this
    # cluster. The worker happens to thread modules/conda_source/conda_env from
    # clusters.yaml (resolve-submit-inputs / scaffold-spec do), but the raw
    # primitive must not require the caller to re-supply config the framework
    # already knows. When NONE of the three is supplied, fall back to the
    # cluster's config — mirroring scaffold_spec's coherent-pair discipline:
    # only emit the conda_env when its conda_source is also present (an env
    # without a source crashes the preamble, #281), and space-join the modules
    # list into the single ``$MODULES`` string the preamble iterates.
    _modules = spec.modules
    _conda_source = spec.conda_source
    _conda_env = spec.conda_env
    if _modules is None and _conda_source is None and _conda_env is None and _cluster_cfg:
        _cfg_modules = _cluster_cfg.get("modules") or []
        if isinstance(_cfg_modules, list) and _cfg_modules:
            _modules = " ".join(str(m) for m in _cfg_modules)
        _cfg_source = _cluster_cfg.get("conda_source")
        _cfg_envs = _cluster_cfg.get("conda_envs") or []
        if _cfg_source and isinstance(_cfg_envs, list) and _cfg_envs:
            _conda_source = str(_cfg_source)
            _conda_env = str(_cfg_envs[0])
    _activation = resolve_activation(
        cluster_cfg=_cluster_cfg,
        modules=_modules,
        conda_source=_conda_source,
        conda_env=_conda_env,
    )
    modules = _activation.modules
    conda_source = _activation.conda_source
    conda_env = _activation.conda_env
    runtime = spec.runtime
    campaign_id = spec.campaign_id or ""
    # BUG 4: materialize the campaign manifest's strategy.params as HPC_KW_*.
    # A Path-B strategy tasks.py reads its knobs from os.environ["HPC_KW_*"];
    # the manifest stores them under strategy.params but nothing wired them
    # together, so the LOCAL enumeration (which imports tasks.py to compute the
    # task list / cmd_sha) and the CLUSTER job ran under default knobs. Carry
    # them into BOTH (a) the PROCESS env — but only TRANSIENTLY, around the local
    # enumeration that reads them (``_resolve_kwargs_keys``, below), restored in
    # a ``finally`` — AND (b) the job_env (assembled below), so the cluster job
    # carries them too. A previous version mutated ``os.environ`` permanently
    # here, leaking one campaign's knobs into every LATER enumeration in the same
    # process; the mutation is now scoped to its sole consumer. The cluster
    # dispatcher already exports resolve()-kwargs as HPC_KW_* (dispatch.py:815);
    # this is the symmetric missing half for STRATEGY params. No-op for a
    # non-campaign submit (empty dict → no env writes, no job_env additions).
    _campaign_kw_env = _campaign_strategy_kw_env(experiment_dir, campaign_id)
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
        # Sibling guard (Move 1 / proving-run #2): a per-task one-liner placed in
        # EXECUTOR breaks the cpu_array.sh transport — the inverse of
        # write-run-sidecar's guard, which refuses a *dispatcher*-shaped value in
        # the *sidecar*. The per-task command belongs in the sidecar's executor.
        _check_executor_is_dispatcher(str(extra_env["EXECUTOR"]))

    # S5 / incident 6: REPO_DIR is derived from the SAME value the rsync deploy
    # lands at (``remote_path``) via the single deploy-target derivation, so the
    # cluster-side ``cd "$REPO_DIR"`` and the rsync destination cannot diverge by
    # construction. ``deploy_target_for`` is the one owner of that identity
    # (infra/backends/_remote_base.py) — also the source of the backend's
    # ``remote_repo`` and the post-deploy existence preflight, so all four anchor
    # on one string. The equality is re-asserted after the ``extra_env`` merge
    # below, where a stale/hand-rolled divergent ``REPO_DIR`` override would land.
    from hpc_agent.infra.backends._remote_base import deploy_target_for

    repo_dir = deploy_target_for(remote_path)

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
        "REPO_DIR": repo_dir,
        "MODULES": modules,
        "CONDA_SOURCE": conda_source,
        "CONDA_ENV": conda_env,
    }
    if runtime == "uv":
        job_env["HPC_RUNTIME"] = "uv"
    if campaign_id:
        job_env["HPC_CAMPAIGN_ID"] = campaign_id
        # BUG 4 half (b): carry the campaign's strategy.params to the CLUSTER
        # job as HPC_KW_* (the same dict already exported into the local process
        # env above for enumeration). Stamped before extra_env so an explicit
        # caller override still wins on key collision.
        if _campaign_kw_env:
            job_env.update(_campaign_kw_env)
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

    # data-trace T3: the digest classifier decides — NO KNOB. Export
    # ``HPC_TRACE_DIGESTS`` AFTER the extra_env merge so the sidecar-derived
    # classification is authoritative (a caller cannot smuggle the flag in via
    # extra_env; the ONLY lever is the typed ``trace_digests`` override). The
    # dispatcher stays dumb — it reads this env var and pays for the ``digest``
    # atom or not; the POLICY lives here, in the submit path (the HPC_TASK_INCLUDE
    # threading precedent). ``is_local=False``: build-submit-spec assembles a
    # CLUSTER submit; the local-runner path classifies its own context.
    from hpc_agent.execution.mapreduce.data_trace_contract import TRACE_DIGEST_ENV_VAR
    from hpc_agent.state.data_trace_classifier import DigestContext, classify_digests

    # ``is_canary=False``: build-submit-spec assembles the MAIN array's job_env.
    # ``spec.canary`` is "gate the main array with a 1-task canary first", NOT
    # "this run IS a canary". The canary reuses this job_env with HPC_TASK_COUNT=1
    # and forces digests ON at its own seam (submit_flow._submit_fresh_canary), so
    # the canary flag is applied where the canary env is built, not here.
    _digest_decision = classify_digests(
        DigestContext(
            is_canary=False,
            reproduces=spec.reproduces is not None,
            is_local=False,
            task_count=int(total_tasks),
            override=spec.trace_digests,
        )
    )
    job_env[TRACE_DIGEST_ENV_VAR] = "1" if _digest_decision.digests_on else "0"

    # S5 / incident 6: REPO_DIR ↔ deploy-target invariant. ``REPO_DIR`` was set
    # from ``deploy_target_for(remote_path)`` — the SAME derivation rsync_push /
    # deploy_runtime / the backend's ``remote_repo`` use — so it equals the rsync
    # destination by construction. The ONE way it can now diverge is a divergent
    # ``REPO_DIR`` threaded through ``extra_env`` (a stale/hand-rolled override or
    # a cached spec field), which the merge above would have just applied. That is
    # exactly the 2026-06 live-canary drift: REPO_DIR pointed at ``…/hpc-demo``
    # while rsync deployed to ``…/demo-hpc`` and every task's ``cd "$REPO_DIR"``
    # missed the executor. Assert the post-merge value still equals the derived
    # deploy target; on mismatch refuse at the build boundary so the drift never
    # reaches a scheduler round-trip. (A caller that legitimately set REPO_DIR to
    # the SAME value as remote_path passes unchanged — back-compat preserved.)
    _effective_repo_dir = job_env.get("REPO_DIR", "")
    if _effective_repo_dir.rstrip("/") != repo_dir:
        raise errors.SpecInvalid(
            f"repo_dir_deploy_mismatch: job_env['REPO_DIR']={_effective_repo_dir!r} "
            f"diverges from the deploy target {repo_dir!r} derived from "
            f"remote_path={remote_path!r}. The cluster-side dispatch runs "
            '`cd "$REPO_DIR" && <executor>`, but rsync deploys the tree to '
            "remote_path — a divergent REPO_DIR (typically a stale extra_env "
            "override or a cached spec field) lands the per-task command in a "
            "directory the executor was never deployed to, the failure the "
            "2026-06 live canary hit (dispatcher_failed: REPO_DIR=…/hpc-demo vs "
            "deployed …/demo-hpc). Drop the REPO_DIR override from extra_env; it "
            "is derived from remote_path automatically."
        )

    # S5 / incident 6 (static, zero-network): the executor's referenced file
    # must be part of the bundle rsync will actually deploy under remote_path. A
    # file that is present locally but stripped by an rsync exclude (or simply
    # absent from the deploy set) lands NO file at REPO_DIR, so the per-task
    # ``cd "$REPO_DIR" && python <file>.py`` fails on the cluster exactly as a
    # REPO_DIR drift would. Catch it at build time, before any network round-trip.
    _check_executor_in_deploy_manifest(
        job_env.get("EXECUTOR", ""),
        experiment_dir=experiment_dir,
        rsync_excludes=rsync_excludes,
    )

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
        # ``_resolve_kwargs_keys`` imports the user's tasks.py and calls
        # resolve(0), which — for a Path-B strategy — reads its knobs from
        # os.environ["HPC_KW_*"]. Set the campaign's strategy params on the
        # process env ONLY for the duration of that enumeration, then restore, so
        # a campaign's knobs never leak into a later build in the same process.
        import os as _os

        _saved_env = {k: _os.environ.get(k) for k in _campaign_kw_env}
        _os.environ.update(_campaign_kw_env)
        try:
            _kwargs_keys = _resolve_kwargs_keys(experiment_dir)
        finally:
            for _k, _v in _saved_env.items():
                if _v is None:
                    _os.environ.pop(_k, None)
                else:
                    _os.environ[_k] = _v
        _check_executor_var_references(
            _effective_executor,
            job_env_keys=set(job_env),
            kwargs_keys=_kwargs_keys,
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
