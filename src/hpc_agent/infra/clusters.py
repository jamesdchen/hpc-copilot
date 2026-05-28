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
from pathlib import Path
from typing import Any, Literal

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, computed_field

from hpc_agent import errors
from hpc_agent.infra.constraints import ClusterConstraints, parse_constraints

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

    scheduler: Literal["sge", "slurm"] = Field(
        description="Scheduler family. Routes the submission to the right backend."
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
