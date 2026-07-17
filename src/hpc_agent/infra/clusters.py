"""Load cluster definitions from clusters.yaml.

Also home to a small set of typed validator helpers for survival-shaped
fields (cold-start mem buffer, NFS staging, walltime arbitrage,
auto-daisy-chain, max walltime). Each helper applies a default and
raises ``ValueError`` on a wrong-typed yaml value, so e.g. a string
``"yes"`` where a bool is expected fails loudly at load time rather
than silently disabling the feature.
"""

from __future__ import annotations

import difflib
import logging
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from hpc_agent import errors
from hpc_agent.infra.constraints import ClusterConstraints, parse_constraints

if TYPE_CHECKING:
    from hpc_agent.state.run_record import RunRecord

_log = logging.getLogger(__name__)

# Scheduler families the framework ships golden profiles for. A
# ``scheduler`` value outside this set is permitted only when the entry
# also carries a pinned ``scheduler_profile``, OR when a loaded plugin
# has registered a backend under that name (see the validators below).
_KNOWN_SCHEDULER_FAMILIES = frozenset({"slurm", "sge", "pbspro", "torque"})

# ---------------------------------------------------------------------------
# ClusterConfig — single Pydantic SoT for the clusters.yaml shape.
# v2 audit BUG-7V2-7: previously the schema was split across three
# disagreeing sources (CLUSTER_YAML_KEYS prose manifest here,
# ALLOWED_CLUSTER_KEYS frozenset in the boundary test, and the actual
# yaml). CLUSTER_YAML_KEYS even declared ``ssh_target`` as a key but no
# real yaml entry used it — code derived it dynamically from
# ``f"{user}@{host}"``. This model is now the canonical SoT; the prose
# manifest is derived from ``model_fields``, and the boundary test
# allowlists are derived too.
# ---------------------------------------------------------------------------


class ClusterConfig(BaseModel):
    """One cluster's entry in clusters.yaml.

    Wire SoT. ``load_clusters_config`` validates each per-cluster entry
    through this model and returns the validated dict so back-compat
    callers (which still consume plain dicts) keep working. New code
    should call ``ClusterConfig.model_validate(cfg)`` to get a typed
    handle with the ``ssh_target`` computed property.
    """

    # Allow forward-compat extras so a user with a newer yaml field
    # doesn't get a hard validation failure — the framework's typed
    # accessors only consume declared fields.
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    scheduler: str = Field(
        description=(
            "Scheduler family. Routes the submission to the right backend. "
            "A known family (``slurm``/``sge``/``pbspro``/``torque``) or a "
            "backend name registered by an installed plugin needs nothing "
            "else; any other value is permitted ONLY when "
            "``scheduler_profile`` pins a concrete SchedulerProfile dict."
        )
    )
    scheduler_profile: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Pinned SchedulerProfile dict. When present it overrides / "
            "augments the golden family profile and is the profile that "
            "gets registered for this cluster. Round-trips through "
            "``SchedulerProfile.from_dict`` at load time so a malformed "
            "pin fails loudly. Required when ``scheduler`` is neither a "
            "known family (``slurm``/``sge``/``pbspro``/``torque``) nor a "
            "plugin-registered backend name."
        ),
    )
    host: str | None = Field(
        default=None,
        description=(
            "Hostname / OpenSSH alias for the cluster. Combined with "
            "``user`` to derive ``ssh_target``. When ``login_pool`` is also "
            "set, this is the DEFAULT / first login node of the pool."
        ),
    )
    login_pool: list[str] = Field(
        default_factory=list,
        description=(
            "Additional login-node hostnames that serve the SAME scheduler + "
            "scratch as ``host`` (a login POOL for one cluster). When the SSH "
            "circuit breaker classifies the active login node as "
            "preamble-degraded (or its circuit opens) and a healthy pool member "
            "exists, the run auto-fails-over to the next member as a journaled, "
            "disclosed host-retarget — same run, jobs, scratch, scheduler; only "
            "the login node moves. Empty (the default) = single-host, unchanged."
        ),
    )
    user: str | None = Field(
        default=None,
        description="SSH username on the cluster. Combined with ``host`` to derive ``ssh_target``.",
    )
    scratch: str | None = Field(
        default=None, description="Per-user scratch directory on the cluster."
    )
    modules: list[str] = Field(
        default_factory=list,
        description="``module load`` arguments injected into the cluster preamble.",
    )
    conda_source: str | None = Field(
        default=None,
        description="Absolute path to the cluster's ``conda.sh`` for ``source $conda_source``.",
    )
    conda_envs: list[str] = Field(
        default_factory=list,
        description="Conda env names to ``conda activate`` (in order) inside the preamble.",
    )
    env_python: str | None = Field(
        default=None,
        description=(
            "Absolute (or ``~``-relative) path to the cluster env's DIRECT "
            "Python interpreter, e.g. ``~/.conda/envs/<name>/bin/python`` or "
            "``/u/local/apps/.../envs/<name>/bin/python``. When set, the LOCAL "
            "control-plane commands run over SSH (status reporter, combiner, "
            "reduce, liveness, canary polls) prepend this interpreter's bin dir "
            "to PATH and skip the ``module load … && source …/conda.sh && conda "
            "activate`` ceremony entirely — that process-spawning preamble is "
            "exactly what hangs on a degraded /apps mount (run-13 finding 10 / "
            "run-14 discovery2). The module/conda preamble still runs inside the "
            "GENERATED JOB SCRIPT on compute nodes. Omit to keep the legacy "
            "preamble on the control plane (disclosed). A ``~`` is expanded by "
            "the REMOTE shell (never quoted here — the MSYS/tilde trap)."
        ),
    )
    gpu_types: list[str] = Field(
        default_factory=list,
        description="GPU types available on this cluster (informational; surfaced to the planner).",
    )
    default_partition: str | None = Field(
        default=None,
        description="Default partition/queue name when the spec doesn't supply one.",
    )
    account: str | None = Field(
        default=None,
        description="Slurm account / project to charge against (``--account=``).",
    )
    gpu_constraint: str | None = Field(
        default=None,
        description="Slurm constraint expression for GPU-type selection (``--constraint=``).",
    )
    constraints: dict[str, Any] = Field(
        default_factory=dict,
        description="Cluster-level resource ceilings (cpus / gpus / mem_mb / walltime_sec).",
    )
    cold_start_mem_buffer: float = Field(
        default=0.15,
        ge=0,
        description="Fractional headroom grown onto the user's --mem ask cold-start.",
    )
    nfs_data_dir: str | None = Field(
        default=None,
        description="When set, threaded through as $HPC_NFS_DATA_DIR for node-local SSD staging.",
    )
    walltime_arbitrage: bool = Field(
        default=True, description="Enable cold-start walltime trim for backfill leverage."
    )
    auto_daisy_chain: bool | None = Field(
        default=None,
        description="Tri-state daisy-chain control: True=always, False=never, None=detect.",
    )
    max_walltime_sec: int = Field(
        default=86400, gt=0, description="Cluster's hard walltime ceiling in seconds."
    )
    default_walltime_sec: int | None = Field(
        default=None,
        description=(
            "Cold-start walltime ask in seconds, used on the very first submit "
            "when no measured runtime prior is available yet (an optional "
            "prior-reading verb, when installed, would otherwise supply it). When "
            "unset the resolver falls back to a conservative built-in default, "
            "clamped to max_walltime_sec — see get_default_walltime_sec."
        ),
    )
    max_node_mem_mb: int | None = Field(
        default=None, description="Largest single-node memory ask the scheduler will accept."
    )
    gpu_queues: dict[str, Any] = Field(
        default_factory=dict,
        description="Per-cluster GPU-queue map for live scoring (SGE).",
    )
    excluded_gpu_queue_prefixes: list[str] = Field(
        default_factory=list,
        description="GPU queue-name prefixes to skip during live scoring (SGE).",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ssh_target(self) -> str | None:
        """Derive ``user@host``. Returns None if either component is missing.

        Historical clusters.yaml entries never set ``ssh_target``
        directly even though CLUSTER_YAML_KEYS once declared it — code
        always computed ``f"{user}@{host}"``. Expose it as a computed
        field so the rest of the framework can stop re-deriving it.
        """
        if not self.host or not self.user:
            return None
        return f"{self.user}@{self.host}"

    @field_validator("scheduler_profile")
    @classmethod
    def _validate_scheduler_profile(cls, value: dict | None) -> dict | None:
        """Round-trip a pinned ``scheduler_profile`` through the spine model.

        When a cluster pins a profile dict we want a malformed pin (a
        missing required field, a wrong-typed regex) to fail loudly here
        at config-load time rather than deep in the submit path. We feed
        the dict to ``SchedulerProfile.from_dict`` purely for its side
        effect of raising on a bad shape; the original dict is returned
        unchanged so the registered profile is exactly what the operator
        wrote.

        The spine module (``hpc_agent.infra.backends.profile``) may not
        exist yet during the parallel refactor. We import it lazily and,
        on ``ImportError``, skip the structural check rather than reject
        a valid pin just because the validator can't be loaded yet.

        Schema validation: rejects a non-dict pin and (when the spine is
        present) any dict that ``SchedulerProfile.from_dict`` can't build.
        """
        if value is None:
            return None
        if not isinstance(value, dict):
            raise errors.SpecInvalid(
                f"scheduler_profile must be a mapping when set, got "
                f"{value!r} ({type(value).__name__})"
            )
        try:
            from hpc_agent.infra.backends.profile import SchedulerProfile
        except ImportError:
            # TODO(Phase-3): spine's profile module not present yet during
            # the parallel refactor — skip the structural round-trip and
            # accept the dict as-is. Once the spine lands this becomes a
            # hard validation point.
            return value
        try:
            SchedulerProfile.from_dict(value)
        except Exception as exc:  # noqa: BLE001 — surface as a config error
            raise errors.SpecInvalid(
                f"scheduler_profile is not a valid SchedulerProfile: {exc}"
            ) from exc
        return value

    @field_validator("scheduler")
    @classmethod
    def _validate_scheduler_family(cls, value: str) -> str:
        """Reject an empty ``scheduler`` value early.

        The known-family-or-pinned cross-field rule lives in
        ``_require_pin_for_unknown_family`` (a model validator) because it
        needs to see ``scheduler_profile`` too; here we only catch the
        degenerate empty/blank value so the cross-field check can assume a
        usable string.
        """
        if not isinstance(value, str) or not value.strip():
            raise errors.SpecInvalid(f"scheduler must be a non-empty string, got {value!r}")
        return value

    @model_validator(mode="after")
    def _require_pin_for_unknown_family(self) -> ClusterConfig:
        """Enforce: an unresolvable scheduler name must carry a pinned profile.

        Three ways a ``scheduler`` value is resolvable, checked cheapest
        first: a known family (``slurm``/``sge``/``pbspro``/``torque``)
        ships a golden profile; a pinned ``scheduler_profile`` dict is its
        own seed; and a name some loaded plugin registered via
        ``@register`` resolves through the backend registry (the
        crowd-compute seam — ``docs/proposals/crowd-compute-backend.md``).
        Anything else would leave the submit path with nothing to
        register, so it is rejected here at config-load time. The
        registry lookup is deliberately last: it imports backend (and
        plugin) modules, which the two cheap checks usually avoid.

        This is a model (cross-field) validator rather than a
        ``scheduler_profile`` field validator because the field defaults
        to ``None`` and field validators don't fire for an omitted field —
        the check must run even when ``scheduler_profile`` is absent
        entirely.

        Schema validation: rejects an unknown ``scheduler`` value that
        lacks both a ``scheduler_profile`` pin and a registered backend.
        """
        if not isinstance(self.scheduler, str):
            return self
        name = self.scheduler.strip().lower()
        if name in _KNOWN_SCHEDULER_FAMILIES or self.scheduler_profile is not None:
            return self
        from hpc_agent.infra.backends import registered_backend_names

        if name in registered_backend_names():
            return self
        raise errors.SpecInvalid(
            f"scheduler {self.scheduler!r} is not a known family "
            f"({sorted(_KNOWN_SCHEDULER_FAMILIES)}) or a registered backend; "
            f"such an entry must either set 'scheduler_profile' to pin a "
            f"concrete SchedulerProfile dict, or install the plugin that "
            f"registers backend {name!r}."
        )


def _allowed_cluster_keys() -> frozenset[str]:
    """Canonical set of allowed top-level keys in a clusters.yaml entry.

    Derived from ``ClusterConfig.model_fields`` so the boundary
    contract test, the prose manifest, and the validator all stay in
    sync from one SoT.
    """
    return frozenset(ClusterConfig.model_fields.keys())


def _field_default_for_manifest(info: Any) -> Any:
    """Project a Pydantic FieldInfo to a JSON-safe default value.

    Returns:
      - ``None`` for required fields (no default declared).
      - ``None`` for ``default_factory`` fields (the factory result is
        not necessarily JSON-stable; callers care about "what shape"
        not "what literal").
      - The literal default value otherwise.
    """
    from pydantic_core import PydanticUndefined

    if info.is_required():
        return None
    default = info.default
    if default is PydanticUndefined:
        return None
    # default_factory produces a fresh instance per call; surface as None.
    if getattr(info, "default_factory", None) is not None:
        return None
    return default


def _annotation_to_str(annotation: Any) -> str:
    """Compact human-readable rendering of a Pydantic field annotation."""
    name = getattr(annotation, "__name__", None)
    if isinstance(name, str):
        return name
    return str(annotation).replace("typing.", "")


# B-M4: declarative manifest of per-cluster yaml keys, derived from
# the Pydantic ClusterConfig model. Surfaced through cmd_capabilities
# so a campus user can discover every supported field. The manifest's
# entries are computed at import time from the model — adding a new
# field to ``ClusterConfig`` automatically updates the manifest.
CLUSTER_YAML_KEYS: list[dict[str, Any]] = [
    {
        "key": name,
        "type": _annotation_to_str(info.annotation),
        "default": _field_default_for_manifest(info),
        "required": info.is_required(),
        "description": info.description or "",
    }
    for name, info in ClusterConfig.model_fields.items()
]


# Original prose manifest kept below (commented out) so ``--help`` text
# in cmd_capabilities can be enriched without re-walking the docstrings.
# The Pydantic-derived manifest above is the canonical surface.


def load_clusters_config(path: Path | None = None) -> dict[str, Any]:
    """Load cluster definitions from clusters.yaml.

    Searches (in order):
    1. Explicit *path* argument
    2. ``HPC_CLUSTERS_CONFIG`` env var (full path to a yaml file)
    3. ``~/.hpc-agent/clusters.yaml`` — user-level config shared across
       every experiment repo (sibling of ``~/.hpc-agent/config.json``,
       which ``recall`` already reads). Cluster connection details are
       infrastructure, not per-experiment data, so the natural home is
       one shared file rather than a per-repo copy. Used only when it
       exists; otherwise falls through to the packaged default.
    4. ``config/clusters.yaml`` shipped inside the ``hpc_agent`` package
    """
    if path is None:
        env_path = os.environ.get("HPC_CLUSTERS_CONFIG")
        if env_path:
            path = Path(env_path)
        else:
            user_path = Path("~/.hpc-agent/clusters.yaml").expanduser()
            if user_path.is_file():
                path = user_path
            else:
                from hpc_agent import _PACKAGE_ROOT

                path = Path(_PACKAGE_ROOT) / "config" / "clusters.yaml"
    with open(path, encoding="utf-8") as f:
        # yaml.safe_load returns None for an empty file; coerce to {} so
        # downstream `.get(...)` calls on the result don't AttributeError.
        result: dict[str, Any] = yaml.safe_load(f) or {}
    return result


def resolve_ssh_target(record: RunRecord) -> str:
    """Resolve the LIVE ``user@host`` for *record*'s cluster at USE time.

    run12 finding 23 / upstream-fixes RULING 1 (option b, "resolve at use
    time"): the journal records the CLUSTER key — that is HISTORY, what the
    human approved. ``user@host`` is *config*: it is resolved fresh from
    ``clusters.yaml`` on every transport-consuming call. So a login-node
    failover (or any host change) is a config edit — change
    ``clusters.yaml[cluster].host`` (or the record's one ``cluster`` key) and
    every consumer picks up the new target with NO journal surgery.

    ``record.ssh_target`` is still WRITTEN at submit (honest provenance of what
    was used then), but consumers no longer TRUST it: this resolver returns the
    value derived from config and only FALLS BACK to the frozen
    ``record.ssh_target`` when config can't answer — the cluster key is absent
    from ``clusters.yaml`` (an ad-hoc cluster, or one removed after submit), the
    entry yields no derivable ``user@host`` (no ``user``/``host``), the config
    can't be loaded, or the record predates the ``cluster`` field entirely. That
    fallback IS the migration shim — no record is ever rewritten.

    The fallback is DISCLOSED on the log (the existing surface — no new wire
    field): a WARNING names the reason, and a live resolution that DIFFERS from
    the frozen submit-time value is logged at INFO so a host retarget leaves a
    trail. Best-effort throughout — a bad/missing ``clusters.yaml`` degrades to
    the frozen value rather than breaking status / monitor / aggregate.
    """
    frozen = str(getattr(record, "ssh_target", "") or "")
    cluster = str(getattr(record, "cluster", "") or "").strip()
    if not cluster:
        # Record predates the cluster field (or it was never populated) — the
        # frozen submit-time target is the only identity we have.
        _log.warning(
            "resolve_ssh_target: record carries no cluster key; using frozen "
            "submit-time ssh_target %r (retarget by config is unavailable for "
            "this record)",
            frozen,
        )
        return frozen
    try:
        cfg = load_clusters_config().get(cluster)
    except Exception as exc:  # noqa: BLE001 — a bad/missing config must not break transport
        _log.warning(
            "resolve_ssh_target: could not load clusters.yaml for cluster %r "
            "(%s); using frozen submit-time ssh_target %r",
            cluster,
            exc,
            frozen,
        )
        return frozen
    if not cfg:
        _log.warning(
            "resolve_ssh_target: cluster %r absent from clusters.yaml; using "
            "frozen submit-time ssh_target %r (add the cluster to clusters.yaml "
            "to retarget its host at use time)",
            cluster,
            frozen,
        )
        return frozen
    # Pool-aware (login_pool): a per-run failover (pool_failover / host-retarget)
    # patches the record's FROZEN ssh_target to the chosen POOL MEMBER. Honor it
    # so the choice sticks at every use-time resolution instead of snapping back
    # to the config default host. Gated to actual pools (host + ≥1 sibling); a
    # single-host entry falls straight through to the config-wins derivation
    # below unchanged (RULING 1). The user stays config-driven (host is the only
    # per-run degree of freedom a pool grants); the member must still validate,
    # and removing a member from the pool hands control back to config.
    pool = _effective_login_pool(cfg)
    frozen_host = frozen.rsplit("@", 1)[-1].strip() if frozen else ""
    pool_user = str(cfg.get("user") or "").strip()
    if len(pool) > 1 and frozen_host and frozen_host in pool and pool_user:
        candidate = f"{pool_user}@{frozen_host}"
        try:
            from hpc_agent.infra.ssh_validation import validate_ssh_target as _validate

            _validate(candidate)
        except errors.SpecInvalid:
            pass  # a bad member → fall through to the config-default derivation
        else:
            if frozen_host != pool[0]:
                _log.info(
                    "resolve_ssh_target: cluster %r active login-pool member is %r "
                    "(default is %r) — using the per-run failover choice",
                    cluster,
                    frozen_host,
                    pool[0],
                )
            return candidate
    try:
        live = ClusterConfig.model_validate(cfg).ssh_target
    except Exception as exc:  # noqa: BLE001 — a malformed entry must not break transport
        _log.warning(
            "resolve_ssh_target: clusters.yaml[%r] failed validation (%s); using "
            "frozen submit-time ssh_target %r",
            cluster,
            exc,
            frozen,
        )
        return frozen
    if not live:
        _log.warning(
            "resolve_ssh_target: clusters.yaml[%r] yields no derivable user@host "
            "(missing user/host); using frozen submit-time ssh_target %r",
            cluster,
            frozen,
        )
        return frozen
    try:
        # A derivation the transport would refuse is NOT a live resolution —
        # the packaged clusters.yaml TEMPLATE carries '<your_user>@...'
        # placeholders, and handing one to ssh_argv turns every consumer into
        # a SpecInvalid crash (CI has only the template). An unconfigured
        # entry falls back to the frozen value like every other can't-answer
        # case. Imported lazily to keep this config module import-light.
        from hpc_agent.infra.ssh_validation import validate_ssh_target as _validate

        _validate(live)
    except errors.SpecInvalid:
        _log.warning(
            "resolve_ssh_target: clusters.yaml[%r] resolves to %r, which is not "
            "a usable ssh target (an unconfigured template placeholder?); using "
            "frozen submit-time ssh_target %r",
            cluster,
            live,
            frozen,
        )
        return frozen
    if live != frozen:
        _log.info(
            "resolve_ssh_target: cluster %r now resolves to %r (frozen "
            "submit-time value was %r) — using the live config target",
            cluster,
            live,
            frozen,
        )
    return live


def writable_clusters_config_path() -> Path:
    """The clusters.yaml path safe to WRITE (to cache a resolved profile).

    Mirrors :func:`load_clusters_config`'s search but only ever returns a
    *writable* user/env location — never the packaged read-only default
    under ``hpc_agent/config/``. Returns ``HPC_CLUSTERS_CONFIG`` when set,
    else ``~/.hpc-agent/clusters.yaml``.
    """
    env_path = os.environ.get("HPC_CLUSTERS_CONFIG")
    if env_path:
        return Path(env_path)
    return Path("~/.hpc-agent/clusters.yaml").expanduser()


def write_back_scheduler_profile(cluster_name: str, profile_dict: dict[str, Any]) -> bool:
    """Best-effort: cache a resolved ``scheduler_profile`` into clusters.yaml.

    Sets ``data[cluster_name]["scheduler_profile"] = profile_dict`` in the
    writable config so a later experiment on the same cluster skips
    re-resolution. Returns ``True`` on success; ``False`` (never raises)
    when there is no writable target or the cluster has no existing entry to
    attach to — an *ad-hoc* cluster (absent from clusters.yaml) relies on
    the per-run ``experiment_meta.json`` pin instead, which is the source of
    truth regardless.
    """
    try:
        target = writable_clusters_config_path()
        data: dict[str, Any] = {}
        if target.is_file():
            loaded = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
            if not isinstance(loaded, dict):
                return False
            data = loaded
        entry = data.get(cluster_name)
        if not isinstance(entry, dict):
            # Only attach to an existing entry — inventing a cluster record
            # from a resolve would be surprising and could mask a typo.
            return False
        entry["scheduler_profile"] = profile_dict
        data[cluster_name] = entry
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")
        return True
    except (OSError, yaml.YAMLError):
        return False


# Substrings whose presence in a ``module load`` argument is honest evidence
# that the module puts the ``conda`` command on PATH (finding 24). ``conda``
# covers anaconda / miniconda; ``mamba`` covers mamba / micromamba / mambaforge;
# ``miniforge`` covers the conda-forge installer (whose name carries neither
# ``conda`` nor ``mamba``). Deliberately NOT the bare token ``forge`` — that
# would false-match the unrelated ``forge`` / Linaro-Forge debugger module. A
# module named ``gcc/11`` or ``cuda/12`` matches nothing here, so it is NOT
# accepted as proof conda is available — the exact hole the pre-tightening
# ``or self.modules`` check let through.
_CONDA_MODULE_TOKENS: tuple[str, ...] = ("conda", "mamba", "miniforge")


def _modules_provide_conda(modules: str) -> bool:
    """Whether the space-joined ``$MODULES`` string loads a conda distribution.

    A deliberately narrow, honest heuristic: a module whose name contains
    ``conda`` (anaconda/miniconda/miniforge) or ``mamba`` (mamba/micromamba)
    puts the ``conda`` command on PATH; an arbitrary module (``gcc/11``,
    ``cuda/12.3``) does not. It can still be fooled — a site could expose conda
    under a bespoke module name, or ship a ``conda-docs`` module that provides
    no binary — so it is proof-shaped, not proof; the honest claim is only that
    a *non-conda* module list is not evidence conda is available.
    """
    low = (modules or "").lower()
    return any(tok in low for tok in _CONDA_MODULE_TOKENS)


@dataclass(frozen=True)
class Activation:
    """A coherent cluster env-activation unit (#281).

    The cluster preamble (``hpc_preamble.sh``) sets the job env up from three
    vars — ``$MODULES`` / ``$CONDA_SOURCE`` / ``$CONDA_ENV``. Threading those
    as three *independent* strings let an incoherent partial state through:
    ``conda_env`` set with ``conda_source`` empty passes a naive "at least one
    is non-empty" guard, then the preamble skips ``source $CONDA_SOURCE`` and
    crashes every task at ``conda activate $CONDA_ENV`` → ``conda: command not
    found`` (the 2026-06-05 Hoffman2 canary, job 13556972). Resolving the
    three as ONE value object with the coherence invariant enforced at
    construction rejects the incoherent states at the boundary instead of
    re-validating them case-by-case at each call site that assembles the fields.

    The invariant is honest about its own reach (finding 24): the earlier
    docstring claimed ``conda: command not found`` was made *unrepresentable*,
    but the check accepted ``conda_env`` set alongside a *non-conda* module list
    (``gcc/11``) with no ``conda_source`` — a module that does not put conda on
    PATH, so the preamble still crashed. The conda_env branch now requires
    positive evidence conda is reachable: an explicit ``conda_source`` OR a
    conda-naming module (:func:`_modules_provide_conda`). A bespoke
    module-provides-conda name it cannot recognise is the residual it does not
    claim to cover — hence "rejects the incoherent states it can see", not
    "makes the crash unrepresentable".

    Construct via :func:`resolve_activation`, which back-fills ``conda_source``
    from ``clusters.yaml`` when a ``conda_env`` is selected but the source was
    dropped — the data the agent kept losing (#281).
    """

    modules: str = ""
    conda_source: str = ""
    conda_env: str = ""

    def __post_init__(self) -> None:
        # The illegal states are rejected at construction, so no downstream
        # call site has to re-assert the invariant by hand.
        if not (self.modules or self.conda_source or self.conda_env):
            raise errors.SpecInvalid(
                "submission has no env-activation declared: modules, conda_source, "
                "and conda_env are all empty. The cluster-side preamble would skip "
                "every env-setup step and run whatever python the SSH login shell "
                "happens to inherit, which usually fails. Populate at least one of "
                "these in clusters.yaml (commonly `conda_source` + `conda_envs`, "
                "or `modules`) and re-run `hpc-agent setup --cluster <name>` to "
                "regenerate the resolved spec."
            )
        # A conda_env needs conda actually on PATH. A conda_source proves it; a
        # module list proves it ONLY when a module names a conda distribution
        # (finding 24) — a non-conda module (`gcc/11`) does not, and the preamble
        # would still die `conda: command not found`.
        if self.conda_env and not self.conda_source and not _modules_provide_conda(self.modules):
            has_modules = bool(self.modules.strip())
            module_note = (
                f" The modules {self.modules!r} name no conda distribution "
                "(anaconda/miniconda/miniforge/mamba), so they are not evidence "
                "conda is on PATH."
                if has_modules
                else " Both conda_source and modules are empty."
            )
            raise errors.SpecInvalid(
                f"conda_env={self.conda_env!r} requires conda on PATH: either a "
                f"conda_source, or a module that loads a conda distribution."
                f"{module_note} The cluster-side preamble would skip `source "
                "$CONDA_SOURCE` (because $CONDA_SOURCE is empty) and then fail at "
                "`conda activate $CONDA_ENV` with `conda: command not found`, "
                "crashing every task in the array. Either populate `conda_source` "
                "in clusters.yaml (commonly "
                "`/u/local/apps/anaconda3/<ver>/etc/profile.d/conda.sh`) and re-run "
                "`hpc-agent setup --cluster <name>`, load a conda-providing module "
                "(e.g. `anaconda3/2024.06`), or drop `conda_env` if activation is "
                "handled by a non-conda module."
            )

    def as_job_env(self) -> dict[str, str]:
        """The three preamble env vars, as the job_env carries them."""
        return {
            "MODULES": self.modules,
            "CONDA_SOURCE": self.conda_source,
            "CONDA_ENV": self.conda_env,
        }


def resolve_activation(
    *,
    cluster_cfg: dict[str, Any] | None,
    modules: str | None = None,
    conda_source: str | None = None,
    conda_env: str | None = None,
) -> Activation:
    """Resolve a coherent :class:`Activation` from caller fields + cluster config.

    Caller-supplied values win; the cluster config is the back-fill source.
    The load-bearing move (#281): when a ``conda_env`` is selected but
    ``conda_source`` was left empty, back-fill it from
    ``cluster_cfg['conda_source']`` — clusters.yaml already carries the right
    source, so an agent that drops it (the 2026-06-05 incident) still gets a
    coherent spec instead of a crashing one. Only when the cluster ALSO has no
    source (and no module puts conda on PATH) does the :class:`Activation`
    invariant fire and refuse at the boundary, before any rsync or qsub.
    """
    cfg = cluster_cfg or {}
    resolved_modules = (modules or "").strip()
    resolved_conda_env = (conda_env or "").strip()
    resolved_conda_source = (conda_source or "").strip()
    if resolved_conda_env and not resolved_conda_source:
        resolved_conda_source = str(cfg.get("conda_source") or "").strip()
    return Activation(
        modules=resolved_modules,
        conda_source=resolved_conda_source,
        conda_env=resolved_conda_env,
    )


def remote_activation_prefix(cluster_cfg: dict[str, Any], *, conda_env: str | None = None) -> str:
    """Build a shell prefix that activates the cluster env for a direct
    (control-plane) ssh command.

    The job-submission path activates the env inside ``hpc_preamble.sh``
    via ``$MODULES`` / ``$CONDA_SOURCE`` / ``$CONDA_ENV``. But control-plane
    commands run directly on the login node by ``ssh_run`` (the status
    reporter, the combiner) never source that preamble — so without this
    they fall to the login node's bare ``python`` (often ``/usr/bin/python``),
    which lacks the framework package (the ``No module named ...`` failure
    class).

    Returns a prefix ending in `` && `` (so it slots after ``cd <path> &&``),
    or ``""`` when nothing is configured — preserving the pre-existing
    bare-python behaviour. *conda_env* (the per-run resolved env from the
    run sidecar) overrides; otherwise the first ``conda_envs`` entry is used.
    The ``<your_env>`` placeholder is treated as unset.

    The ``conda activate`` is emitted whenever a conda env is configured AND
    conda is EVIDENCED reachable — via an explicit ``source <conda_source>`` OR a
    conda-NAMING ``module load`` (:func:`_modules_provide_conda`, the
    module-provided-conda configuration). This is the SAME finding-24 predicate
    the :class:`Activation` invariant gates *acceptance* on, so accept-at-submit
    and activate-at-control-plane are ONE definition of "conda reachable" (G6):
    a non-conda module (``gcc/11``) is not evidence, a spec pairing it with a
    ``conda_env`` is refused at submit, so the control-plane must not emit a
    doomed ``conda activate`` for it either. Gating the activate on
    ``conda_source`` alone had left every control-plane command on a module-conda
    cluster running under the login node's bare ``python`` (``No module named
    ...`` / rc 127); a cluster with neither a source nor a conda-naming module
    has no way to reach conda, so it still emits no (doomed) ``conda activate``.
    """
    parts: list[str] = []
    modules = cluster_cfg.get("modules") or []
    for mod in modules:
        parts.append(f"module load {shlex.quote(str(mod))}")
    conda_source = cluster_cfg.get("conda_source")
    if conda_source:
        parts.append(f"source {shlex.quote(str(conda_source))}")
    # "Conda is reachable" is the finding-24 predicate the ``Activation``
    # invariant shares: an explicit ``conda_source`` OR a conda-naming module.
    conda_reachable = bool(conda_source) or _modules_provide_conda(
        " ".join(str(m) for m in modules)
    )
    if conda_reachable:
        env = conda_env or next(iter(cluster_cfg.get("conda_envs") or []), None)
        if env and env != "<your_env>":
            parts.append(f"conda activate {shlex.quote(str(env))}")
    if not parts:
        return ""
    return " && ".join(parts) + " && "


# Conservative shell-safe charset for a bin dir emitted UNQUOTED into the
# control-plane command. Unquoted is deliberate: an ``env_python`` may be
# ``~``-relative, and the REMOTE shell must expand the ``~`` — ``shlex.quote``
# would freeze it, and tilde-expansion does not happen inside double quotes
# either (the MSYS/tilde trap). Only these chars are allowed through; anything
# else (a space, a shell metachar) falls back to the legacy preamble rather than
# risk an injection or a mis-expansion.
_ENV_PYTHON_SAFE = re.compile(r"[A-Za-z0-9._~/+-]+")


def _control_plane_direct_prefix(env_python: str) -> str | None:
    """A preamble-FREE control-plane prefix that puts *env_python*'s bin dir on
    PATH, or ``None`` when *env_python* is unusable (fall back to the preamble).

    Returns ``export PATH=<bindir>:"$PATH" && `` so the command's literal
    ``python``/``python3`` token resolves to the cluster env's interpreter with
    NO ``module load`` / ``source …/conda.sh`` / ``conda activate`` — the
    process-spawning ceremony that hangs on a degraded /apps mount (run-13
    finding 10 / run-14). A leading ``~`` survives unquoted so the remote shell
    (not the local MSYS one) expands it; a bin dir with any shell-unsafe
    character yields ``None`` (legacy preamble, disclosed).

    ``export PATH=~/x:"$PATH"`` expands the ``~`` because tilde-expansion DOES
    fire after ``=`` (and after each ``:``) in an assignment word, even though it
    would NOT inside the double quotes — hence only ``$PATH`` is quoted.
    """
    p = (env_python or "").strip()
    if not p or "/" not in p:
        return None
    bindir = p.rsplit("/", 1)[0]
    if not bindir:
        return None
    m = _ENV_PYTHON_SAFE.fullmatch(bindir)
    if m is None:
        return None
    return f'export PATH={bindir}:"$PATH" && '


def effective_login_pool(cluster_cfg: dict[str, Any]) -> list[str]:
    """The ordered, de-duplicated login-node pool for a cluster: ``[host,
    *login_pool]`` with blanks/placeholders dropped.

    Single-host entries (no ``login_pool``) return ``[host]`` (or ``[]`` when no
    host) — so every pool-aware branch is a strict no-op for them. Public:
    consumed cross-package by ``ops.host_retarget.pool_failover``.
    """
    out: list[str] = []
    for h in [cluster_cfg.get("host"), *(cluster_cfg.get("login_pool") or [])]:
        s = str(h or "").strip()
        if s and s != "<your_host>" and s not in out:
            out.append(s)
    return out


_effective_login_pool = effective_login_pool  # back-compat alias (pre-promotion name)


def remote_activation_for_sidecar(
    sidecar: dict[str, Any], *, fallback_cluster: str | None = None
) -> str:
    """Activation prefix for a run's control-plane ssh command — the ONE
    definition of "how to activate on this cluster", consulted by every
    reporter / reconcile / combine call.

    The control-plane commands (status reporter, combiner, reconcile probe)
    run directly on the login node and never source ``hpc_preamble.sh``, so
    they need the conda / module activation built inline
    (:func:`remote_activation_prefix`). Per-field precedence — so an empty
    sidecar env can never blind the reporter, *even for an already-damaged run
    whose hand-carried sidecar dropped fields* (proving-run-5 finding 13):

      1. **Explicit sidecar activation wins** — a ``modules`` /
         ``conda_source`` / ``conda_env`` the sidecar pins is honored
         (back-compat; also survives an ad-hoc cluster absent from
         clusters.yaml, or a config that drifted after submit).
      2. **Else derive from the cluster** — activation is a cluster-local fact
         (#281): each field the sidecar omits is back-filled from
         ``clusters.yaml[cluster]``. A hand-authored sidecar that dropped
         ``env.conda_env`` (or the whole ``env`` block) but kept ``cluster``
         still activates — activation must never depend on a field a sidecar
         can drop, which is precisely what fell to a bare ``python`` (``exit
         127``) in finding 13.
      3. **Else** ``""`` (bare ``python``, unchanged) for a sidecar that
         carries neither a usable activation nor a resolvable cluster.

    Best-effort throughout: a bad / missing clusters.yaml degrades to ``""``
    rather than breaking status / aggregate.
    """
    env = sidecar.get("env") or {}
    # Sidecar-pinned fields. ``modules`` is written as a list OR a space-joined
    # string across callers (see ``write_run_sidecar``) — normalise to the list
    # ``remote_activation_prefix`` iterates.
    raw_modules = env.get("modules")
    if isinstance(raw_modules, str):
        pinned_modules = raw_modules.split()
    elif isinstance(raw_modules, list):
        pinned_modules = [str(m) for m in raw_modules]
    else:
        pinned_modules = []
    pinned_conda_source = env.get("conda_source")
    pinned_conda_env = env.get("conda_env")

    # The cluster config is BOTH the tier-2 derive source and the back-fill for
    # a sidecar that pins only *some* fields (a sidecar carrying just conda_env
    # still gets the cluster's conda_source — the pre-existing behaviour).
    cfg: dict[str, Any] = {}
    # Every submit-flow sidecar today carries NO ``cluster`` (run #7), so tier-2
    # backfill would never fire — callers pass the run record's cluster as
    # *fallback_cluster* to close that. Consolidates the seed that was
    # copy-pasted into verify_canary / record_status; new consumers (aggregate /
    # reconcile) get the same via one param instead of a fourth/fifth copy.
    cluster_key = sidecar.get("cluster") or fallback_cluster
    if cluster_key:
        try:
            cfg = load_clusters_config().get(cluster_key, {}) or {}
        except Exception:  # noqa: BLE001 — a bad/missing config must not break status/aggregate
            cfg = {}

    # PREAMBLE-FREE control plane (run-13 finding 10 / run-14): when a DIRECT
    # env interpreter is known (sidecar-pinned ``env_python`` wins, else the
    # cluster's), the control-plane command needs no ``module load`` / ``source
    # …/conda.sh`` / ``conda activate`` at all — that process-spawning ceremony
    # is exactly what hung every submit-s2 worker on discovery2's degraded /apps
    # mount. A PATH-prepend to the interpreter's bin dir makes the command's
    # literal ``python``/``python3`` resolve to the env interpreter directly. The
    # module/conda preamble still runs inside the GENERATED JOB SCRIPT on compute
    # nodes (untouched), and a cluster with no derivable ``env_python`` keeps the
    # legacy preamble below — disclosed on the log. Command-class disclosure via
    # the module logger (the surface transport already discloses on, cf.
    # resolve_ssh_target).
    env_python = str(env.get("env_python") or cfg.get("env_python") or "").strip()
    direct = _control_plane_direct_prefix(env_python)
    if direct is not None:
        _log.info(
            "control-plane activation: DIRECT env interpreter %r (preamble-free — "
            "no module/conda ceremony) for cluster %r",
            env_python,
            cluster_key or "<ad-hoc>",
        )
        return direct
    if env_python:
        _log.info(
            "control-plane activation: env_python %r for cluster %r is not "
            "shell-safe to emit — falling back to the legacy module/conda preamble",
            env_python,
            cluster_key or "<ad-hoc>",
        )

    # Per-field precedence: the sidecar pin wins; the cluster back-fills what
    # the sidecar omitted; neither present → the field stays empty and
    # ``remote_activation_prefix`` returns "" (tier 3, bare python).
    merged = {
        "modules": pinned_modules or (cfg.get("modules") or []),
        "conda_source": pinned_conda_source or cfg.get("conda_source"),
        "conda_envs": cfg.get("conda_envs") or [],
    }
    return remote_activation_prefix(merged, conda_env=pinned_conda_env)


def validate_clusters_config(clusters: dict[str, Any]) -> None:
    """Validate every per-cluster entry through ``ClusterConfig``.

    Raises :class:`errors.ConfigInvalid` (with the offending cluster
    name in the message) on any schema violation. Opt-in: callers that
    want the strong contract invoke this after ``load_clusters_config``.
    ``load_clusters_config`` itself stays lax so existing tests that
    construct partial dicts (for gpu-loader / fairshare-loader edge
    cases) keep working.
    """
    for cluster_name, cluster_cfg in list(clusters.items()):
        if not isinstance(cluster_cfg, dict):
            continue
        try:
            ClusterConfig.model_validate(cluster_cfg)
        except Exception as exc:
            raise errors.ConfigInvalid(
                f"clusters.yaml entry {cluster_name!r} failed validation: {exc}"
            ) from exc


def near_miss_cluster_keys(entry: dict[str, Any]) -> dict[str, list[str]]:
    """Map each unrecognized key in *entry* to close-matching allowed keys.

    ``ClusterConfig`` uses ``extra='ignore'`` for forward-compat, so a misspelled
    key (``conda_env`` for ``conda_envs``, ``nfs-data-dir`` for ``nfs_data_dir``)
    is silently dropped and the feature it meant to enable never fires — a class
    ``validate_clusters_config`` can't catch (unknown keys are ignored, not
    rejected). This surfaces the LIKELY typos: an unknown key that is a near-miss
    (``difflib``, cutoff 0.7) of a real one. A wholly novel forward-compat key
    (no close match) is deliberately NOT reported, so the allowlist can lag a new
    field without a false alarm. Returns ``{unknown_key: [suggestions]}`` (empty
    when every key is recognized).
    """
    allowed = _allowed_cluster_keys()
    out: dict[str, list[str]] = {}
    for key in entry:
        if not isinstance(key, str) or key in allowed:
            continue
        close = difflib.get_close_matches(key, sorted(allowed), n=3, cutoff=0.7)
        if close:
            out[key] = close
    return out


def load_constraints(
    cluster_config: dict,
    profile_config: dict | None = None,
) -> ClusterConstraints:
    """Merge cluster-level and profile-level constraints.

    Profile constraints override cluster constraints field-by-field.
    Missing fields use cluster defaults, then ClusterConstraints defaults.
    """
    merged = {**cluster_config.get("constraints", {})}
    if profile_config is not None:
        merged.update(profile_config.get("constraints", {}))
    return parse_constraints(merged)


def get_cold_start_mem_buffer(
    cluster_config: dict[str, Any],
    *,
    default: float = 0.15,
) -> float:
    """Read the per-cluster ``cold_start_mem_buffer`` (fractional headroom).

    Returns the fraction by which a campus user's ``--mem`` ask is
    grown when no runtime prior exists for ``(profile, cluster,
    cmd_sha)`` — survival headroom against the OOM daemon for the very
    first run on a new code path. Default ``0.15`` = 15% pad. The
    smart planner takes over once ≥5 successful samples exist per
    GPU type and the buffer stops being applied (priors already
    encode the right safety margin).

    Schema validation: rejects negative values (would shrink the ask)
    but accepts ``0.0`` (legacy "kept user default" behavior).
    """
    raw = cluster_config.get("cold_start_mem_buffer", default)
    try:
        val = float(raw)
    except (TypeError, ValueError) as e:
        raise errors.SpecInvalid(f"cold_start_mem_buffer must be a number, got {raw!r}") from e
    if val < 0:
        raise errors.SpecInvalid(
            f"cold_start_mem_buffer must be non-negative (it grows the ask, "
            f"never shrinks it), got {val}"
        )
    return val


def get_nfs_data_dir(cluster_config: dict[str, Any]) -> str | None:
    """Read the per-cluster ``nfs_data_dir`` if configured.

    When set, the submit-flow injects this path as ``$HPC_NFS_DATA_DIR``
    into the cluster job's env so the template preamble copies it into
    node-local SSD ($SLURM_TMPDIR/$TMPDIR) before the executor runs —
    survival against NFS throttling when N tasks read the same files
    at once. Returns ``None`` when unset (the staging block is gated
    on the env var being present, so omission is a no-op).
    """
    raw = cluster_config.get("nfs_data_dir")
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise errors.SpecInvalid(f"nfs_data_dir must be a non-empty string when set, got {raw!r}")
    return raw


def get_walltime_arbitrage(
    cluster_config: dict[str, Any],
    *,
    default: bool = True,
) -> bool:
    """Read the per-cluster ``walltime_arbitrage`` flag (cold-start trim).

    Default ``True``: the planner trims the user's nominal walltime ask
    by 15min and floors to a 5min boundary when no runtime priors exist
    to construct a smarter recommendation, so the campus user fits in
    backfill shadows the round-number jobs don't reach. Set
    ``walltime_arbitrage: false`` per-cluster to disable on a scheduler
    where the trim isn't beneficial (e.g. a partition without backfill).

    Schema validation: rejects non-bool values so ``"yes"``/``1``/``0``
    don't silently flip the feature on or off.
    """
    raw = cluster_config.get("walltime_arbitrage", default)
    if not isinstance(raw, bool):
        raise errors.SpecInvalid(
            f"walltime_arbitrage must be a bool, got {raw!r} ({type(raw).__name__})"
        )
    return raw


def get_auto_daisy_chain(cluster_config: dict[str, Any]) -> bool | None:
    """Read the per-cluster ``auto_daisy_chain`` flag.

    Three states:

    - ``True``: always auto-daisy-chain when the ask exceeds the
      cluster's max walltime minus a 1h queue-wait buffer. Use this
      when you've verified your executor checkpoints reliably and want
      to skip the per-run detection scan.
    - ``False``: NEVER chain on this cluster — kill switch. The
      "exceeds max walltime" error fires unmodified.
    - Absent (returns ``None``): defer to ``detect_checkpointing``.
      Chain only when past runs of ``(profile, cluster)`` produced
      checkpoint-shaped files; otherwise emit the explanatory error
      so the user can add checkpointing or opt in explicitly.

    Schema validation: rejects non-bool / non-None values.
    """
    if "auto_daisy_chain" not in cluster_config:
        return None
    raw = cluster_config["auto_daisy_chain"]
    if raw is None:
        return None
    if not isinstance(raw, bool):
        raise errors.SpecInvalid(
            f"auto_daisy_chain must be a bool when set, got {raw!r} ({type(raw).__name__})"
        )
    return raw


def get_max_node_mem_mb(cluster_config: dict[str, Any]) -> int | None:
    """Read the per-cluster ``max_node_mem_mb`` (per-node memory ceiling).

    The largest single-node memory request the cluster will schedule.
    When the cold-start buffer (or any other recommender) would push
    the campus user's ``--mem`` ask past this ceiling, the planner
    clamps it back down — without the clamp, an ask like 240GB on a
    256GB node × 1.15 buffer = 276GB sits Pending forever with
    ``ReqNodeNotAvail`` and the user's brand-new run never starts.

    Returns ``None`` when unset; the planner then leaves the ask
    uncapped (legacy behavior).

    Schema validation: rejects non-int / non-positive values. Bools
    are rejected explicitly because ``True == 1`` would otherwise
    silently clamp every ask to 1MB.
    """
    if "max_node_mem_mb" not in cluster_config:
        return None
    raw = cluster_config["max_node_mem_mb"]
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise errors.SpecInvalid(
            f"max_node_mem_mb must be a positive int when set, got {raw!r} ({type(raw).__name__})"
        )
    if raw <= 0:
        raise errors.SpecInvalid(f"max_node_mem_mb must be positive, got {raw}")
    return int(raw)


def get_max_walltime_sec(
    cluster_config: dict[str, Any],
    *,
    default: int = 86400,
) -> int:
    """Read the per-cluster ``max_walltime_sec`` (hard scheduler ceiling).

    The cluster's hard walltime ceiling in seconds. Auto-daisy-chain
    fires when an ask exceeds ``max_walltime_sec - 3600`` (the 1h
    buffer absorbs queue-wait variance between segments). Default
    ``86400`` (24h) is a typical campus-cluster ceiling; verify against
    your scheduler's documented max and override per-cluster.

    Schema validation: rejects non-int / non-positive values.
    """
    raw = cluster_config.get("max_walltime_sec", default)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise errors.SpecInvalid(
            f"max_walltime_sec must be a positive int, got {raw!r} ({type(raw).__name__})"
        )
    if raw <= 0:
        raise errors.SpecInvalid(f"max_walltime_sec must be positive, got {raw}")
    return int(raw)


# Conservative cold-start walltime (seconds) used when a cluster declares no
# ``default_walltime_sec`` and no runtime prior exists yet. 2h is a modest guess
# that still errs long enough to survive queue-wait jitter and a fatter-than-
# expected first task: under-asking gets the job killed at the ceiling (the whole
# run wasted), while over-asking only costs a little queue priority. The value is
# further clamped to max_walltime_sec/4 in get_default_walltime_sec so a large-
# ceiling cluster does not cold-start at a fat fraction of its max. The two-phase
# canary MEASURES the real runtime and shrinks the ACTUAL array walltime
# (ops/submit/canary_calibration.py), so this guess only governs the canary +
# the very first ask, never the sized main array (run-14: a hand-picked 6h ask
# inflated est_core_hours 36x over the measured runtime).
_COLD_START_WALLTIME_SEC = 7200


def get_default_walltime_sec(
    cluster_config: dict[str, Any],
    *,
    floor: int = _COLD_START_WALLTIME_SEC,
) -> int:
    """Resolve the cold-start walltime (seconds) — ALWAYS returns a usable value.

    This is the host's baseline walltime: the value the submit procedure
    uses whenever a measured runtime prior isn't available — on the very
    first submit (no prior yet), or on any install where the optional
    prior-reading verb isn't registered. An optional plugin may offer a
    smarter prior-based walltime, but the host never depends on it: this
    fallback MUST always resolve (#170), or a submit would stall waiting
    on a feature that may not be installed.

    Resolution order:

    1. ``clusters.<name>.default_walltime_sec`` when set — the operator's
       explicit cold-start ask (validated: positive int).
    2. otherwise *floor* (a conservative built-in default), clamped to the
       cluster's ``max_walltime_sec`` so a small-ceiling cluster never gets a
       cold-start ask above what its scheduler will accept.

    Either way the result is clamped to ``max_walltime_sec``.

    Schema validation: rejects non-int / non-positive ``default_walltime_sec``.
    """
    ceiling = get_max_walltime_sec(cluster_config)
    raw = cluster_config.get("default_walltime_sec")
    if raw is None:
        # No explicit cold-start ask: the modest built-in floor, additionally
        # clamped to a quarter of the cluster ceiling so a large-ceiling cluster
        # never cold-starts at a fat fraction of its max (a 1h-max test cluster
        # already can't exceed its ceiling). max(1, ...) keeps a tiny-ceiling
        # cluster's ask positive.
        return min(floor, max(1, ceiling // 4), ceiling)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise errors.SpecInvalid(
            f"default_walltime_sec must be a positive int, got {raw!r} ({type(raw).__name__})"
        )
    if raw <= 0:
        raise errors.SpecInvalid(f"default_walltime_sec must be positive, got {raw}")
    return min(int(raw), ceiling)
