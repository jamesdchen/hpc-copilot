"""GPU queue selection: static fallback list + optional live SGE queue scoring.

Usage (programmatic)::

    from hpc_agent.infra.gpu import pick_gpu

    # Static fallback - returns first available from preferred list
    result = pick_gpu(preferred=["A100", "H200", "A6000", "V100"])

    # Live scoring via local qstat
    result = pick_gpu(preferred=["A100", "H200"], live=True, slots_needed=4)

    # Live scoring via SSH to a remote cluster
    result = pick_gpu(preferred=["A100", "H200"], live=True, ssh_host="user@cluster")

Return shape (uniform)::

    {
        "gpus": [ {"gpu": "<name>", ...}, ... ],   # ordered best-first
        "errors": [ {"code": str, "detail": str}, ... ],
    }

The first entry of ``gpus`` is the top pick; the remainder gives the
LLM-orchestrator visibility into alternatives.  When no GPU qualifies,
``gpus`` is ``[]`` and ``errors`` describes why.

GPU queue config
----------------

The mapping from SGE queue prefix (``gpu_a100``) to canonical GPU name
+ performance weight is configurable per cluster via the ``gpu_queues``
key in ``clusters.yaml``. When unset, the loader falls back to the
Hoffman2-shaped defaults baked here. Pass ``cluster_name=`` (or supply
a fully-formed ``gpu_config=`` dict) when picking a GPU on a cluster
that needs a different queue map.
"""

from __future__ import annotations

__all__ = [
    "pick_gpu",
    "parse_qstat_f",
    "score_gpus",
    "load_gpu_config_for_cluster",
]

import re
import subprocess

from hpc_agent import errors
from hpc_agent.infra.parsing import parse_qstat_columns


def pick_gpu(
    preferred: list[str],
    *,
    live: bool = False,
    ssh_host: str | None = None,
    slots_needed: int = 4,
    exclude: set[str] | None = None,
    gpu_config: dict[str, dict] | None = None,
    cluster_name: str | None = None,
) -> dict:
    """Pick the best available GPU type.

    Parameters
    ----------
    preferred : ordered list of GPU type names (best first).
        Used as the static fallback order and to filter live results.
    live : if True, query ``qstat -f`` for real-time queue occupancy.
    ssh_host : if set, run qstat over SSH (e.g. ``user@cluster``).
    slots_needed : minimum free slots required per GPU type (live mode).
    exclude : GPU type names to skip.
    gpu_config : explicit GPU queue config. Keys are SGE queue prefixes
        (e.g. ``"gpu_a100"``), values are dicts with at least
        ``{"name": "A100", "perf": 1.0}`` plus any extra fields you want
        propagated to the result. Takes precedence over *cluster_name*.
    cluster_name : load the GPU config from ``clusters.yaml`` for this
        cluster (key ``gpu_queues`` on the cluster entry). When neither
        ``gpu_config`` nor ``cluster_name`` is set, falls back to the
        Hoffman2-shaped default.

    Returns
    -------
    ``{"gpus": [{"gpu": ..., ...}, ...], "errors": [{"code": ..., "detail": ...}, ...]}``.
    The first element of ``gpus`` is the top recommendation; subsequent entries
    are alternatives ordered by score.  Empty ``gpus`` means no candidate
    qualified and ``errors`` will explain why.
    """
    exclude = {e.upper() for e in (exclude or set())}
    candidates = [g for g in preferred if g.upper() not in exclude]

    if not candidates:
        return {
            "gpus": [],
            "errors": [{"code": "no_candidates", "detail": "no candidates after exclusions"}],
        }

    if not live:
        return {
            "gpus": [{"gpu": candidates[0], "source": "fallback"}],
            "errors": [],
        }

    # Live mode: query qstat
    qstat_text = _run_qstat(ssh_host)
    if qstat_text is None:
        # qstat failed - fall back to static order
        return {
            "gpus": [{"gpu": candidates[0], "source": "fallback"}],
            "errors": [{"code": "qstat_unavailable", "detail": "qstat could not be run"}],
        }

    cfg = gpu_config
    if cfg is None and cluster_name is not None:
        cfg = load_gpu_config_for_cluster(cluster_name)
    if cfg is None:
        cfg = _DEFAULT_GPU_CONFIG
    excluded = _excluded_prefixes_for_cluster(cluster_name)
    agg = parse_qstat_f(qstat_text, gpu_config=cfg, excluded_prefixes=excluded)
    return score_gpus(
        agg,
        gpu_config=cfg,
        exclude=exclude,
        slots_needed=slots_needed,
        preferred_order=candidates,
    )


def load_gpu_config_for_cluster(cluster_name: str) -> dict[str, dict] | None:
    """Load the ``gpu_queues`` map for *cluster_name* from ``clusters.yaml``.

    Returns ``None`` if the cluster entry has no ``gpu_queues`` key or the
    cluster is unknown — callers should fall back to
    :data:`_DEFAULT_GPU_CONFIG`. Rejects malformed shapes (non-dict
    values, missing ``name`` / ``perf`` keys) with ``ValueError`` so a
    typo fails loudly rather than silently swapping in the default.
    """
    from hpc_agent.infra.clusters import load_clusters_config

    clusters = load_clusters_config()
    cluster_cfg = clusters.get(cluster_name)
    if not isinstance(cluster_cfg, dict):
        return None
    raw = cluster_cfg.get("gpu_queues")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise errors.SpecInvalid(
            f"clusters.yaml: {cluster_name!r}.gpu_queues must be a mapping, "
            f"got {type(raw).__name__}"
        )
    out: dict[str, dict] = {}
    for queue_prefix, entry in raw.items():
        if not isinstance(entry, dict) or "name" not in entry or "perf" not in entry:
            raise errors.SpecInvalid(
                f"clusters.yaml: {cluster_name!r}.gpu_queues.{queue_prefix} "
                "must be a mapping with 'name' and 'perf' keys"
            )
        out[queue_prefix] = entry
    return out


def _excluded_prefixes_for_cluster(cluster_name: str | None) -> set[str]:
    """Resolve the queue-prefix exclusion set for *cluster_name*.

    YAML key: ``excluded_gpu_queue_prefixes`` (list of strings). Falls
    back to :data:`_DEFAULT_EXCLUDED_PREFIXES` if unset or the cluster is
    unknown.
    """
    if cluster_name is None:
        return _DEFAULT_EXCLUDED_PREFIXES
    from hpc_agent.infra.clusters import load_clusters_config

    clusters = load_clusters_config()
    cluster_cfg = clusters.get(cluster_name)
    if not isinstance(cluster_cfg, dict):
        return _DEFAULT_EXCLUDED_PREFIXES
    raw = cluster_cfg.get("excluded_gpu_queue_prefixes")
    if raw is None:
        return _DEFAULT_EXCLUDED_PREFIXES
    if not isinstance(raw, list) or not all(isinstance(p, str) for p in raw):
        raise errors.SpecInvalid(
            f"clusters.yaml: {cluster_name!r}.excluded_gpu_queue_prefixes must be a list of strings"
        )
    return set(raw)


# ---------------------------------------------------------------------------
# Default GPU config (Hoffman2-shaped fallback used when clusters.yaml has
# no ``gpu_queues`` / ``excluded_gpu_queue_prefixes`` entries for the
# target cluster). Override per-cluster via the YAML keys above.
# ---------------------------------------------------------------------------

_DEFAULT_GPU_CONFIG: dict[str, dict] = {
    "gpu_h200": {"name": "H200", "perf": 1.5},
    "gpu_a100": {"name": "A100", "perf": 1.2},
    "gpu_h100": {"name": "H100", "perf": 1.3},
    "gpu_a6000": {"name": "A6000", "perf": 1.0},
    "gpu_l40s": {"name": "L40S", "perf": 1.1},
    "gpu_v100": {"name": "V100", "perf": 0.7},
    "gpu_RTX2080Ti": {"name": "RTX2080Ti", "perf": 0.5},
}

# Queue prefixes to always ignore (default fallback).
_DEFAULT_EXCLUDED_PREFIXES: set[str] = {
    "gpu_P4",
    "gpu_k40",
    "gpu_smp",
    "gpu_test",
    "gpu_rh7",
    "gpu_a100_test",
    "gpu_l40s_multi",
}
# Back-compat alias — older callers may import _EXCLUDED_PREFIXES directly.
_EXCLUDED_PREFIXES: set[str] = _DEFAULT_EXCLUDED_PREFIXES


# ---------------------------------------------------------------------------
# qstat parsing
# ---------------------------------------------------------------------------


def _run_qstat(ssh_host: str | None = None) -> str | None:
    """Run ``qstat -f -q gpu_*``, optionally over SSH. Returns stdout or None.

    When *ssh_host* is set (``"user@cluster"``), routes through the
    canonical :func:`hpc_agent.infra.remote.ssh_run` helper so the
    SSH command picks up the project-wide multiplexing options and
    timeout discipline (``SSH_TIMEOUT_SEC = 60`` by default).
    """
    if ssh_host:
        # Lazy import to avoid a hard dependency for the local-qstat path.
        from hpc_agent.infra.remote import ssh_run  # noqa: PLC0415
        from hpc_agent.infra.ssh_validation import validate_ssh_target  # noqa: PLC0415

        try:
            validate_ssh_target(ssh_host)
        except errors.SpecInvalid:
            # A malformed ssh_host routes into the same graceful
            # qstat-unavailable fallback as a transport failure —
            # ``pick_gpu`` degrades to the static preferred order.
            return None
        try:
            result = ssh_run("qstat -f -q gpu_*", ssh_target=ssh_host)
        except (TimeoutError, OSError):
            return None
        if result.returncode == 0:
            return result.stdout
        return None

    # Local qstat path — no SSH wrapping needed.
    cmd = ["qstat", "-f", "-q", "gpu_*"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=30)
        if result.returncode == 0:
            return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def parse_qstat_f(
    text: str,
    gpu_config: dict[str, dict] | None = None,
    excluded_prefixes: set[str] | None = None,
) -> dict[str, dict]:
    """Parse ``qstat -f`` output into per-GPU-type aggregates.

    Returns ``{queue_prefix: {"used": int, "total": int, "active_nodes": int}}``.

    *excluded_prefixes* defaults to :data:`_DEFAULT_EXCLUDED_PREFIXES`
    (Hoffman2-shaped). Pass an explicit set (typically resolved via
    :func:`_excluded_prefixes_for_cluster`) to honour a cluster's
    ``excluded_gpu_queue_prefixes`` YAML override.
    """
    cfg = gpu_config or _DEFAULT_GPU_CONFIG
    excluded = excluded_prefixes if excluded_prefixes is not None else _DEFAULT_EXCLUDED_PREFIXES
    agg: dict[str, dict] = {}

    # ``parse_qstat_columns`` strips blanks and discards header/separator
    # rows; we additionally filter to lines whose primary column starts
    # with ``gpu_`` (the queue prefix) since qstat -f also emits global
    # status banners.
    for parts in parse_qstat_columns(text, require_min_cols=3):
        # Skip continuation lines (marked by leading "" sentinel).
        if parts and parts[0] == "":
            continue
        if not parts[0].startswith("gpu_"):
            continue

        queue_host = parts[0]  # e.g. gpu_a100.q@g13
        slots_str = parts[2]  # e.g. 0/11/256

        is_disabled = len(parts) >= 6 and "d" in parts[-1]

        queue_name = queue_host.split(".q")[0]
        if queue_name in excluded:
            continue

        config_key = None
        for key in cfg:
            if queue_name == key:
                config_key = key
                break
        if config_key is None:
            continue

        m = re.match(r"(\d+)/(\d+)/(\d+)", slots_str)
        if not m:
            continue

        used = int(m.group(1)) + int(m.group(2))
        total = int(m.group(3))

        if config_key not in agg:
            agg[config_key] = {"used": 0, "total": 0, "active_nodes": 0}

        if not is_disabled and total > 0:
            agg[config_key]["used"] += used
            agg[config_key]["total"] += total
            agg[config_key]["active_nodes"] += 1

    return agg


def score_gpus(
    agg: dict[str, dict],
    *,
    gpu_config: dict[str, dict] | None = None,
    exclude: set[str] | None = None,
    slots_needed: int = 4,
    preferred_order: list[str] | None = None,
) -> dict:
    """Score GPU types by free capacity * performance weight.

    Parameters
    ----------
    agg : output of parse_qstat_f
    gpu_config : queue_prefix -> config dict (must have "name" and "perf")
    exclude : GPU names to skip
    slots_needed : minimum free slots
    preferred_order : if no GPU qualifies, fall back to first in this list

    Returns
    -------
    ``{"gpus": [...], "errors": [...]}`` - ``gpus`` ordered by score (best first),
    or empty with an error describing why no GPU was eligible.
    """
    cfg = gpu_config or _DEFAULT_GPU_CONFIG
    exclude = {e.upper() for e in (exclude or set())}
    # When the caller supplies a preferred list it also *filters* live
    # results (per the public docstring): a GPU type the caller did not ask
    # for must never be recommended just because it scored highest. An empty
    # / absent list means "no preference" → consider every configured type.
    preferred_names = {p.upper() for p in (preferred_order or [])}
    errors: list[dict] = []

    scored: list[dict] = []
    for key, gpu_cfg in cfg.items():
        gpu_name = gpu_cfg["name"]
        if gpu_name.upper() in exclude:
            continue
        if preferred_names and gpu_name.upper() not in preferred_names:
            continue
        stats = agg.get(key)
        if not stats or stats["total"] == 0 or stats.get("active_nodes", 0) == 0:
            continue

        free = stats["total"] - stats["used"]
        util = stats["used"] / stats["total"] if stats["total"] > 0 else 1.0

        if free < slots_needed:
            continue

        # Propagate all extra config fields to the result
        extra = {k: v for k, v in gpu_cfg.items() if k not in ("name", "perf")}
        scored.append(
            {
                "gpu": gpu_name,
                "free_slots": free,
                "utilization": round(util, 3),
                "score": round(free * gpu_cfg.get("perf", 1.0), 1),
                "active_nodes": stats.get("active_nodes", 0),
                "source": "live",
                **extra,
            }
        )

    scored.sort(key=lambda x: x["score"], reverse=True)

    if scored:
        return {"gpus": scored, "errors": errors}

    # Fallback: lowest utilization
    fallback: list[dict] = []
    for key, gpu_cfg in cfg.items():
        gpu_name = gpu_cfg["name"]
        if gpu_name.upper() in exclude:
            continue
        if preferred_names and gpu_name.upper() not in preferred_names:
            continue
        stats = agg.get(key)
        if not stats or stats["total"] == 0:
            continue
        util = stats["used"] / stats["total"]
        extra = {k: v for k, v in gpu_cfg.items() if k not in ("name", "perf")}
        fallback.append(
            {
                "gpu": gpu_name,
                "free_slots": stats["total"] - stats["used"],
                "utilization": round(util, 3),
                "score": 0,
                "source": "live",
                **extra,
            }
        )
    fallback.sort(key=lambda x: x["utilization"])

    if fallback:
        errors.append(
            {
                "code": "insufficient_free_slots",
                "detail": "no GPU had enough free slots; scheduling may be delayed",
            }
        )
        return {"gpus": fallback, "errors": errors}

    # Nothing from live data - use preferred order
    if preferred_order:
        errors.append(
            {
                "code": "no_live_gpus",
                "detail": "no eligible GPU queues in qstat output",
            }
        )
        return {
            "gpus": [{"gpu": preferred_order[0], "source": "fallback"}],
            "errors": errors,
        }

    errors.append({"code": "no_eligible_gpus", "detail": "no eligible GPU queues found"})
    return {"gpus": [], "errors": errors}
