"""Load cluster definitions from clusters.yaml.

Also home to a small set of typed validator helpers for survival-shaped
fields (cold-start mem buffer, NFS staging, walltime arbitrage,
auto-daisy-chain, max walltime). Each helper applies a default and
raises ``ValueError`` on a wrong-typed yaml value, so e.g. a string
``"yes"`` where a bool is expected fails loudly at load time rather
than silently disabling the feature.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from hpc_agent import errors
from hpc_agent.infra.constraints import ClusterConstraints, parse_constraints

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
            "``user`` to derive ``ssh_target``."
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

                path = _PACKAGE_ROOT / "config" / "clusters.yaml"
    with open(path, encoding="utf-8") as f:
        # yaml.safe_load returns None for an empty file; coerce to {} so
        # downstream `.get(...)` calls on the result don't AttributeError.
        result: dict[str, Any] = yaml.safe_load(f) or {}
    return result


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
    construction makes that state *unrepresentable* — you cannot build an
    ``Activation`` the preamble would crash on — instead of validating against
    it case-by-case at each call site that assembles the fields.

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
        if self.conda_env and not (self.conda_source or self.modules):
            raise errors.SpecInvalid(
                f"conda_env={self.conda_env!r} requires either conda_source or a "
                "modules entry that puts conda on PATH; both are empty. The "
                "cluster-side preamble would skip `source $CONDA_SOURCE` (because "
                "$CONDA_SOURCE is empty) and then fail at `conda activate "
                "$CONDA_ENV` with `conda: command not found`, crashing every task "
                "in the array. Either populate `conda_source` in clusters.yaml "
                "(commonly `/u/local/apps/anaconda3/<ver>/etc/profile.d/conda.sh`) "
                "and re-run `hpc-agent setup --cluster <name>`, or drop `conda_env` "
                "if env activation is handled by a module."
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
    """
    parts: list[str] = []
    for mod in cluster_cfg.get("modules") or []:
        parts.append(f"module load {shlex.quote(str(mod))}")
    conda_source = cluster_cfg.get("conda_source")
    if conda_source:
        parts.append(f"source {shlex.quote(str(conda_source))}")
        env = conda_env or next(iter(cluster_cfg.get("conda_envs") or []), None)
        if env and env != "<your_env>":
            parts.append(f"conda activate {shlex.quote(str(env))}")
    if not parts:
        return ""
    return " && ".join(parts) + " && "


def remote_activation_for_sidecar(sidecar: dict[str, Any]) -> str:
    """Activation prefix for a run, derived from its sidecar's ``cluster``
    + resolved ``env``. Best-effort: ``""`` when the cluster can't be
    resolved (so the caller falls back to bare ``python``, unchanged)."""
    cluster_key = sidecar.get("cluster")
    if not cluster_key:
        return ""
    try:
        cfg = load_clusters_config().get(cluster_key, {})
    except Exception:  # noqa: BLE001 — a bad/missing config must not break status/aggregate
        return ""
    conda_env = (sidecar.get("env") or {}).get("conda_env")
    return remote_activation_prefix(cfg, conda_env=conda_env)


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
# ``default_walltime_sec`` and no runtime prior exists yet. 4h matches the
# CPU/ML resource default in the submit procedure and errs long on purpose:
# under-asking gets the job killed at the walltime ceiling (the whole run
# wasted), while over-asking only costs a little queue priority. The canary and
# first real run establish a prior fast, after which the planner takes over.
_COLD_START_WALLTIME_SEC = 14400


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
        # No explicit cold-start ask: clamp the conservative floor to the
        # cluster ceiling (a 1h-max test cluster must not get a 4h default).
        return min(floor, ceiling)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise errors.SpecInvalid(
            f"default_walltime_sec must be a positive int, got {raw!r} ({type(raw).__name__})"
        )
    if raw <= 0:
        raise errors.SpecInvalid(f"default_walltime_sec must be positive, got {raw}")
    return min(int(raw), ceiling)
