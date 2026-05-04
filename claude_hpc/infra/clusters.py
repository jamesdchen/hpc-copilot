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
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from claude_hpc.orchestrator.constraints import ClusterConstraints, parse_constraints


def load_clusters_config(path: Path | None = None) -> dict[str, Any]:
    """Load cluster definitions from clusters.yaml.

    Searches (in order):
    1. Explicit *path* argument
    2. ``HPC_CLUSTERS_CONFIG`` env var (full path to a yaml file)
    3. ``config/clusters.yaml`` shipped inside the ``claude_hpc`` package
    """
    if path is None:
        env_path = os.environ.get("HPC_CLUSTERS_CONFIG")
        if env_path:
            path = Path(env_path)
        else:
            from claude_hpc import _PACKAGE_ROOT

            path = _PACKAGE_ROOT / "config" / "clusters.yaml"
    with open(path) as f:
        # yaml.safe_load returns None for an empty file; coerce to {} so
        # downstream `.get(...)` calls on the result don't AttributeError.
        result: dict[str, Any] = yaml.safe_load(f) or {}
        return result


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
        raise ValueError(f"cold_start_mem_buffer must be a number, got {raw!r}") from e
    if val < 0:
        raise ValueError(
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
        raise ValueError(f"nfs_data_dir must be a non-empty string when set, got {raw!r}")
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
        raise ValueError(f"walltime_arbitrage must be a bool, got {raw!r} ({type(raw).__name__})")
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
        raise ValueError(
            f"auto_daisy_chain must be a bool when set, got {raw!r} ({type(raw).__name__})"
        )
    return raw


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
        raise ValueError(
            f"max_walltime_sec must be a positive int, got {raw!r} ({type(raw).__name__})"
        )
    if raw <= 0:
        raise ValueError(f"max_walltime_sec must be positive, got {raw}")
    return int(raw)
