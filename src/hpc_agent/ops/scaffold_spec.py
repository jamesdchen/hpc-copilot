"""``scaffold-spec`` primitive — a context-populated ``--spec`` skeleton (#287).

When the worker (or any agent caller) needs to invoke a verb that takes a
``--spec`` JSON, it has no way to obtain a valid skeleton: each missing
field, wrong type, or stray ``extra=forbid`` key surfaces ONE at a time as
a ``spec_invalid`` envelope, and the agent walks the schema by failed-
validation feedback (the 2026-06-05 demo burned 11 rounds on
``resolve-submit-inputs`` alone). ``scaffold-spec`` breaks that loop: it
composes the existing read-only context sources —

    load-context  +  clusters.yaml  +  compute-run-id  +  discover-executors

— into a populated, **schema-valid** skeleton for the named verb, then
validates the result against that verb's own input model before returning.
The few fields context cannot supply come back as schema-valid placeholders
listed in ``unresolved_fields``; the agent overrides those and invokes.

``verb="query"`` — read-only, no side effects (it never writes the spec to
disk; the skeleton rides the envelope ``data``, like ``prepare-phase2-spec``).

Coverage: the four verbs #287 names. The three submit-family verbs with
measured divination loops in the demo — ``build-submit-spec`` (7 rounds),
``validate-campaign`` (3), ``resolve-submit-inputs`` (11) — plus
``campaign-run``, which composes three nested *workflow* specs
(submit-pipeline → submit-and-verify → submit-flow, status-pipeline →
monitor-flow, aggregate-flow). campaign-run is the worst offender to
hand-build, so scaffolding its full nested structure is the biggest win.

I/O contracts:

* Input: flags only (``--verb`` / ``--cluster`` / ``--run-name`` /
  ``--from-context``); no ``--spec``, so no input schema.
* Output: a :class:`ScaffoldSpecResult` matching
  ``schemas/scaffold_spec.output.json``.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

import pydantic

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.actions.build_submit_spec import BuildSubmitSpecInput
from hpc_agent._wire.queries.scaffold_spec import ScaffoldSpecResult
from hpc_agent._wire.workflows.campaign_run import CampaignRunSpec
from hpc_agent._wire.workflows.resolve_submit_inputs import ResolveSubmitInputsSpec
from hpc_agent._wire.workflows.validate_campaign import ValidateCampaignSpec
from hpc_agent.cli._dispatch import CliArg, CliShape
from hpc_agent.incorporation.build.compute_run_id import compute_run_id
from hpc_agent.infra.clusters import ClusterConfig, load_clusters_config
from hpc_agent.meta.campaign.atoms.load_context import load_context
from hpc_agent.state.discover import discover_executors

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

__all__ = ["scaffold_spec"]

# Valid scheduler backends (mirrors ``_wire._shared.BackendName``); a
# clusters.yaml ``scheduler`` outside this set falls back to a placeholder.
_BACKENDS = ("sge", "slurm", "pbspro", "torque")

# Schema-valid placeholders for required fields context can't supply. Each
# satisfies its target field's constraint so the skeleton still validates;
# the caller is told to replace them via ``unresolved_fields``.
_PH_RUN_ID = "PLACEHOLDER-run-id"  # ^[A-Za-z0-9._\-]+$
_PH_CMD_SHA = "0" * 8  # ^[0-9a-f]{8,64}$
_PH_SSH = "USER@HOST"  # ^[^@]+@[^@]+$
_PH_REMOTE = "/scratch/PLACEHOLDER"
_PH_PROFILE = "PLACEHOLDER"
_PH_CLUSTER = "PLACEHOLDER"
_PH_BACKEND = "slurm"
_PH_EXECUTOR = "python executor.py"
_PH_RESULT_DIR = "results/{run_id}/task_{task_id}"
_PH_JOB_NAME = "PLACEHOLDER_array"
_PH_SCRIPT = ".hpc/templates/cpu_array.sh"


@dataclasses.dataclass
class _Acc:
    """Accumulator threaded through the scaffolders.

    Records per-field provenance (``sources``), the placeholder fields the
    caller must fill (``unresolved``), and context-gathering degradations
    (``warnings``).
    """

    sources: dict[str, str] = dataclasses.field(default_factory=dict)
    unresolved: list[str] = dataclasses.field(default_factory=list)
    warnings: list[str] = dataclasses.field(default_factory=list)

    def req(
        self,
        target: dict[str, Any],
        key: str,
        value: Any,
        source: str,
        placeholder: Any,
        *,
        prefix: str = "",
    ) -> None:
        """Set a REQUIRED field: real value when present, else a placeholder (kept unresolved)."""
        path = f"{prefix}{key}"
        if value is not None and value != "":
            target[key] = value
            self.sources[path] = source
        else:
            target[key] = placeholder
            self.sources[path] = f"placeholder — {source} unavailable"
            self.unresolved.append(path)

    def opt(
        self, target: dict[str, Any], key: str, value: Any, source: str, *, prefix: str = ""
    ) -> None:
        """Set an OPTIONAL field only when context supplied a non-empty value."""
        if value is not None and value != "" and value != []:
            target[key] = value
            self.sources[f"{prefix}{key}"] = source


@dataclasses.dataclass
class _Context:
    """Read-only context assembled once, shared across the scaffolders."""

    cluster_name: str | None
    cluster_cfg: ClusterConfig | None
    run_name: str | None
    latest_run: dict[str, Any]
    run_id: str | None
    cmd_sha: str | None


def _gather_context(
    experiment_dir: Path, cluster: str | None, run_name: str | None, acc: _Acc
) -> _Context:
    """Compose the read-only sources (#287 step 1-4) into a :class:`_Context`.

    Every source is best-effort: a missing clusters.yaml entry, an absent
    ``.hpc/tasks.py`` (so ``compute-run-id`` can't run), or a bare
    experiment dir degrade to placeholders + a warning, never an error.
    """
    ctx_data = load_context(experiment_dir=experiment_dir)
    latest: dict[str, Any] = ctx_data.get("latest_run") or {}

    clusters: dict[str, Any] = {}
    try:
        clusters = load_clusters_config()
    except Exception as exc:  # noqa: BLE001 — degrade, don't fail a scaffold
        acc.warnings.append(f"clusters.yaml unreadable; cluster fields are placeholders: {exc}")

    cluster_name = cluster or latest.get("cluster")
    if cluster_name is None and len(clusters) == 1:
        cluster_name = next(iter(clusters))
        acc.warnings.append(
            f"no --cluster given; defaulted to the only configured cluster {cluster_name!r}"
        )

    cluster_cfg: ClusterConfig | None = None
    if cluster_name and cluster_name in clusters:
        try:
            cluster_cfg = ClusterConfig.model_validate(clusters[cluster_name])
        except Exception as exc:  # noqa: BLE001 — degrade, don't fail a scaffold
            acc.warnings.append(
                f"cluster {cluster_name!r} config invalid; its fields are placeholders: {exc}"
            )
    elif cluster_name and clusters:
        acc.warnings.append(
            f"cluster {cluster_name!r} not found in clusters.yaml; its fields are placeholders"
        )

    executors: list[str] = []
    try:
        executors = [e.name for e in discover_executors(experiment_dir) if e.is_executor]
    except Exception as exc:  # noqa: BLE001 — degrade, don't fail a scaffold
        acc.warnings.append(f"discover-executors failed: {exc}")

    resolved_run_name = (
        run_name or latest.get("profile") or (executors[0] if len(executors) == 1 else None)
    )

    run_id: str | None = None
    cmd_sha: str | None = None
    if resolved_run_name:
        try:
            rid = compute_run_id(experiment_dir, run_name=resolved_run_name)
            run_id, cmd_sha = rid["run_id"], rid["cmd_sha"]
        except errors.HpcError as exc:
            acc.warnings.append(
                f"compute-run-id unavailable (run_id/cmd_sha are placeholders): {exc}"
            )

    return _Context(
        cluster_name=cluster_name,
        cluster_cfg=cluster_cfg,
        run_name=resolved_run_name,
        latest_run=latest,
        run_id=run_id,
        cmd_sha=cmd_sha,
    )


def _valid_task_count(latest: dict[str, Any]) -> int | None:
    """Return ``task_count`` only when it is a real, schema-valid (``>=1``) count."""
    tc = latest.get("task_count")
    return tc if isinstance(tc, int) and tc >= 1 else None


def _build_submit_block(ctx: _Context, acc: _Acc, prefix: str) -> dict[str, Any]:
    """Populate a ``BuildSubmitSpecInput`` dict (top-level or nested under *prefix*)."""
    cfg = ctx.cluster_cfg
    latest = ctx.latest_run
    d: dict[str, Any] = {}
    acc.req(
        d,
        "profile",
        ctx.run_name,
        "load-context latest_run.profile / --run-name",
        _PH_PROFILE,
        prefix=prefix,
    )
    acc.req(
        d,
        "cluster",
        ctx.cluster_name,
        "--cluster / clusters.yaml / load-context",
        _PH_CLUSTER,
        prefix=prefix,
    )
    acc.req(
        d,
        "ssh_target",
        (cfg.ssh_target if cfg else None),
        f"clusters.yaml#{ctx.cluster_name}.ssh_target",
        _PH_SSH,
        prefix=prefix,
    )
    remote = latest.get("remote_path")
    if not remote and cfg and cfg.scratch and ctx.run_name:
        remote = f"{cfg.scratch.rstrip('/')}/{ctx.run_name}"
    acc.req(
        d,
        "remote_path",
        remote,
        "load-context latest_run.remote_path / clusters.yaml#scratch",
        _PH_REMOTE,
        prefix=prefix,
    )
    acc.req(d, "run_id", ctx.run_id, f"compute-run-id({ctx.run_name})", _PH_RUN_ID, prefix=prefix)
    acc.req(
        d, "cmd_sha", ctx.cmd_sha, f"compute-run-id({ctx.run_name})", _PH_CMD_SHA, prefix=prefix
    )
    acc.req(
        d,
        "total_tasks",
        _valid_task_count(latest),
        "load-context latest_run.task_count",
        1,
        prefix=prefix,
    )
    sched = cfg.scheduler if cfg else None
    acc.req(
        d,
        "backend",
        (sched if sched in _BACKENDS else None),
        f"clusters.yaml#{ctx.cluster_name}.scheduler",
        _PH_BACKEND,
        prefix=prefix,
    )
    # Only the COHERENT conda-activation pair (#281: a conda_env without a
    # conda_source crashes the cluster preamble — never emit the half-state).
    if cfg and cfg.conda_source and cfg.conda_envs:
        acc.opt(d, "conda_source", cfg.conda_source, "clusters.yaml#conda_source", prefix=prefix)
        acc.opt(d, "conda_env", cfg.conda_envs[0], "clusters.yaml#conda_envs[0]", prefix=prefix)
    if latest.get("runtime") == "uv":
        acc.opt(d, "runtime", "uv", "load-context latest_run.runtime", prefix=prefix)
    acc.opt(
        d,
        "result_dir_template",
        latest.get("result_dir_template"),
        "load-context latest_run.result_dir_template",
        prefix=prefix,
    )
    acc.opt(
        d,
        "campaign_id",
        latest.get("campaign_id"),
        "load-context latest_run.campaign_id",
        prefix=prefix,
    )
    return d


def _build_sidecar_block(ctx: _Context, acc: _Acc, prefix: str) -> dict[str, Any]:
    """Populate a ``WriteRunSidecarInput`` dict (nested under *prefix*)."""
    latest = ctx.latest_run
    d: dict[str, Any] = {}
    acc.req(d, "run_id", ctx.run_id, f"compute-run-id({ctx.run_name})", _PH_RUN_ID, prefix=prefix)
    acc.req(
        d, "cmd_sha", ctx.cmd_sha, f"compute-run-id({ctx.run_name})", _PH_CMD_SHA, prefix=prefix
    )
    # The REAL per-task command (e.g. `python train.py --seed $SEED`) — not
    # the dispatcher, not derivable from context. Always a placeholder.
    acc.req(
        d,
        "executor",
        None,
        "the REAL per-task command (not derivable from context)",
        _PH_EXECUTOR,
        prefix=prefix,
    )
    acc.req(
        d,
        "result_dir_template",
        latest.get("result_dir_template"),
        "load-context latest_run.result_dir_template",
        _PH_RESULT_DIR,
        prefix=prefix,
    )
    acc.req(
        d,
        "task_count",
        _valid_task_count(latest),
        "load-context latest_run.task_count",
        1,
        prefix=prefix,
    )
    acc.opt(d, "cluster", ctx.cluster_name, "--cluster / load-context", prefix=prefix)
    acc.opt(
        d, "profile", ctx.run_name, "load-context latest_run.profile / --run-name", prefix=prefix
    )
    acc.opt(
        d,
        "remote_path",
        latest.get("remote_path"),
        "load-context latest_run.remote_path",
        prefix=prefix,
    )
    acc.opt(
        d,
        "campaign_id",
        latest.get("campaign_id"),
        "load-context latest_run.campaign_id",
        prefix=prefix,
    )
    acc.opt(
        d, "resources", latest.get("resources"), "load-context latest_run.resources", prefix=prefix
    )
    acc.opt(d, "env", latest.get("env"), "load-context latest_run.env", prefix=prefix)
    if latest.get("runtime") == "uv":
        acc.opt(d, "runtime", "uv", "load-context latest_run.runtime", prefix=prefix)
    return d


def _scaffold_build_submit_spec(ctx: _Context, acc: _Acc) -> dict[str, Any]:
    return _build_submit_block(ctx, acc, "")


def _scaffold_validate_campaign(ctx: _Context, acc: _Acc) -> dict[str, Any]:
    latest = ctx.latest_run
    spec: dict[str, Any] = {}
    acc.req(
        spec, "profile", ctx.run_name, "load-context latest_run.profile / --run-name", _PH_PROFILE
    )
    acc.req(
        spec, "cluster", ctx.cluster_name, "--cluster / clusters.yaml / load-context", _PH_CLUSTER
    )
    acc.opt(
        spec,
        "result_dir_template",
        latest.get("result_dir_template"),
        "load-context latest_run.result_dir_template",
    )
    acc.opt(spec, "campaign_id", latest.get("campaign_id"), "load-context latest_run.campaign_id")
    return spec


def _scaffold_resolve_submit_inputs(ctx: _Context, acc: _Acc) -> dict[str, Any]:
    spec: dict[str, Any] = {}
    acc.req(
        spec, "run_name", ctx.run_name, "load-context latest_run.profile / --run-name", _PH_PROFILE
    )
    spec["submit"] = _build_submit_block(ctx, acc, "submit.")
    spec["sidecar"] = _build_sidecar_block(ctx, acc, "sidecar.")
    # build_tasks is optional (BuildTasksPyInput | None); needed only on a
    # cold start where .hpc/tasks.py is absent and must be scaffolded.
    acc.sources["build_tasks"] = (
        "omitted (optional; supply axes + flags_by_executor only when .hpc/tasks.py is absent)"
    )
    return spec


def _build_submit_flow_block(ctx: _Context, acc: _Acc, prefix: str) -> dict[str, Any]:
    """Populate a ``SubmitFlowSpec`` dict — the deep leaf of campaign-run's submit spine.

    Distinct from ``_build_submit_block`` (which builds ``BuildSubmitSpecInput``,
    the *input* to build-submit-spec): a submit-flow spec carries the assembled
    ``job_name`` / ``script`` / ``job_env`` that build-submit-spec would produce.
    The ``EXECUTOR`` inside ``job_env`` is the real per-task command — not
    derivable from context — so ``job_env`` is a placeholder the caller fills.
    """
    cfg = ctx.cluster_cfg
    latest = ctx.latest_run
    d: dict[str, Any] = {}
    acc.req(
        d,
        "profile",
        ctx.run_name,
        "load-context latest_run.profile / --run-name",
        _PH_PROFILE,
        prefix=prefix,
    )
    acc.req(
        d,
        "cluster",
        ctx.cluster_name,
        "--cluster / clusters.yaml / load-context",
        _PH_CLUSTER,
        prefix=prefix,
    )
    acc.req(
        d,
        "ssh_target",
        (cfg.ssh_target if cfg else None),
        f"clusters.yaml#{ctx.cluster_name}.ssh_target",
        _PH_SSH,
        prefix=prefix,
    )
    remote = latest.get("remote_path")
    if not remote and cfg and cfg.scratch and ctx.run_name:
        remote = f"{cfg.scratch.rstrip('/')}/{ctx.run_name}"
    acc.req(
        d,
        "remote_path",
        remote,
        "load-context latest_run.remote_path / clusters.yaml#scratch",
        _PH_REMOTE,
        prefix=prefix,
    )
    acc.req(
        d,
        "job_name",
        (f"{ctx.run_name}_array" if ctx.run_name else None),
        "derived from run_name",
        _PH_JOB_NAME,
        prefix=prefix,
    )
    acc.req(d, "run_id", ctx.run_id, f"compute-run-id({ctx.run_name})", _PH_RUN_ID, prefix=prefix)
    acc.req(
        d,
        "total_tasks",
        _valid_task_count(latest),
        "load-context latest_run.task_count",
        1,
        prefix=prefix,
    )
    sched = cfg.scheduler if cfg else None
    acc.req(
        d,
        "backend",
        (sched if sched in _BACKENDS else None),
        f"clusters.yaml#{ctx.cluster_name}.scheduler",
        _PH_BACKEND,
        prefix=prefix,
    )
    acc.req(
        d,
        "script",
        None,
        "the cluster job script (.hpc/templates/<cpu|gpu>_array.sh)",
        _PH_SCRIPT,
        prefix=prefix,
    )
    # job_env is required (dict[str, str]); EXECUTOR (the real per-task command)
    # is not derivable, so the whole dict is a placeholder the caller fills.
    d["job_env"] = {"EXECUTOR": _PH_EXECUTOR, "HPC_RUN_ID": ctx.run_id or _PH_RUN_ID}
    acc.sources[f"{prefix}job_env"] = "placeholder — EXECUTOR (real per-task command) not derivable"
    acc.unresolved.append(f"{prefix}job_env")
    if latest.get("runtime") == "uv":
        acc.opt(d, "runtime", "uv", "load-context latest_run.runtime", prefix=prefix)
    return d


def _scaffold_campaign_run(ctx: _Context, acc: _Acc) -> dict[str, Any]:
    # campaign-run nests three composites: submit-pipeline → submit-and-verify →
    # submit-flow (the deep leaf), status-pipeline → monitor-flow, and
    # aggregate-flow. The run identity + cluster fields thread into all three
    # from one gathered context — getting the 3-level nesting right is the part
    # that's hardest to hand-build, which is exactly what scaffold-spec supplies.
    spec: dict[str, Any] = {
        "submit": {
            "submit": {"submit": _build_submit_flow_block(ctx, acc, "submit.submit.submit.")}
        },
        "status": {"monitor": {}},
        "aggregate": {},
    }
    acc.req(
        spec["status"]["monitor"],
        "run_id",
        ctx.run_id,
        f"compute-run-id({ctx.run_name})",
        _PH_RUN_ID,
        prefix="status.monitor.",
    )
    acc.req(
        spec["aggregate"],
        "run_id",
        ctx.run_id,
        f"compute-run-id({ctx.run_name})",
        _PH_RUN_ID,
        prefix="aggregate.",
    )
    acc.opt(
        spec,
        "campaign_id",
        ctx.latest_run.get("campaign_id"),
        "load-context latest_run.campaign_id",
    )
    return spec


# verb -> (scaffolder, target input model). The model double-checks the
# emitted skeleton before it leaves (the #287 "refuses to emit a spec the
# verb would itself reject" guarantee).
_SCAFFOLDERS: dict[str, Callable[[_Context, _Acc], dict[str, Any]]] = {
    "build-submit-spec": _scaffold_build_submit_spec,
    "validate-campaign": _scaffold_validate_campaign,
    "resolve-submit-inputs": _scaffold_resolve_submit_inputs,
    "campaign-run": _scaffold_campaign_run,
}
_TARGET_MODELS: dict[str, type[pydantic.BaseModel]] = {
    "build-submit-spec": BuildSubmitSpecInput,
    "validate-campaign": ValidateCampaignSpec,
    "resolve-submit-inputs": ResolveSubmitInputsSpec,
    "campaign-run": CampaignRunSpec,
}


@primitive(
    name="scaffold-spec",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    cli=CliShape(
        help=(
            "Emit a populated, schema-valid --spec skeleton for another verb "
            "(build-submit-spec / resolve-submit-inputs / validate-campaign / campaign-run), "
            "pulling cluster / run_id / context values from clusters.yaml, "
            "compute-run-id, and load-context so the agent stops divining the "
            "schema one spec_invalid at a time (#287). Read the returned spec, "
            "fill its unresolved_fields, then invoke the target verb."
        ),
        verb="scaffold-spec",
        experiment_dir_arg=True,
        args=(
            CliArg("--verb", type=str, required=True, help="Target verb to scaffold a --spec for."),
            CliArg(
                "--cluster",
                type=str,
                help="clusters.yaml entry (default: latest run's, or the sole configured one).",
            ),
            CliArg(
                "--run-name",
                type=str,
                help="fed to compute-run-id (default: latest run's profile, or the sole executor).",
            ),
            CliArg(
                "--from-context",
                action="store_true",
                help="populate from context (clusters.yaml / compute-run-id / load-context).",
            ),
        ),
    ),
    agent_facing=True,
)
def scaffold_spec(
    *, experiment_dir: Path, verb: str, cluster: str | None = None, run_name: str | None = None
) -> ScaffoldSpecResult:
    """Emit a context-populated, schema-valid ``--spec`` skeleton for *verb*.

    Composes ``load-context`` + ``clusters.yaml`` + ``compute-run-id`` +
    ``discover-executors`` into the target verb's input shape, validates the
    result against that verb's Pydantic model, and returns it with the few
    non-derivable required fields flagged in ``unresolved_fields``.

    Raises :class:`errors.SpecInvalid` for an unsupported *verb* (the
    message names the supported set) or — should a scaffolder ever emit a
    structurally invalid skeleton — for the internal validation failure.
    """
    scaffolder = _SCAFFOLDERS.get(verb)
    if scaffolder is None:
        supported = ", ".join(sorted(_SCAFFOLDERS))
        raise errors.SpecInvalid(
            f"scaffold-spec has no scaffolder for verb {verb!r}. Supported verbs: {supported}."
        )

    acc = _Acc()
    ctx = _gather_context(experiment_dir, cluster, run_name, acc)
    spec = scaffolder(ctx, acc)

    try:
        _TARGET_MODELS[verb].model_validate(spec)
    except pydantic.ValidationError as exc:  # pragma: no cover — guards a scaffolder bug
        raise errors.SpecInvalid(
            f"scaffold-spec produced an invalid {verb} skeleton (internal bug): {exc}"
        ) from exc

    return ScaffoldSpecResult(
        verb=verb,
        spec=spec,
        unresolved_fields=sorted(acc.unresolved),
        sources=acc.sources,
        supported_verbs=sorted(_SCAFFOLDERS),
        warnings=acc.warnings,
    )
