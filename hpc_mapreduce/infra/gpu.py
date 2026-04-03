"""GPU queue selection: static fallback list + optional live SGE queue scoring.

Usage (programmatic)::

    from hpc_mapreduce.infra.gpu import pick_gpu

    # Static fallback — returns first available from preferred list
    gpu = pick_gpu(preferred=["A100", "H200", "A6000", "V100"])

    # Live scoring via local qstat
    gpu = pick_gpu(preferred=["A100", "H200"], live=True, slots_needed=4)

    # Live scoring via SSH to a remote cluster
    gpu = pick_gpu(preferred=["A100", "H200"], live=True, ssh_host="user@cluster")

Returns a dict with at minimum ``{"gpu": "<type>"}`` for static mode,
or full scoring details when ``live=True``.
"""

from __future__ import annotations

__all__ = ["pick_gpu", "parse_qstat_f", "score_gpus"]

import re
import subprocess


def pick_gpu(
    preferred: list[str],
    *,
    live: bool = False,
    ssh_host: str | None = None,
    slots_needed: int = 4,
    exclude: set[str] | None = None,
    gpu_config: dict[str, dict] | None = None,
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
    gpu_config : optional override for GPU queue configs. Keys are SGE queue
        prefixes (e.g. ``"gpu_a100"``), values are dicts with at least
        ``{"name": "A100", "perf": 1.0}`` plus any extra fields you want
        propagated to the result.

    Returns
    -------
    dict with ``"gpu"`` key. In live mode, also includes ``"free_slots"``,
    ``"utilization"``, ``"score"``, and ``"all_scores"``.
    Static mode returns ``{"gpu": "<name>", "source": "fallback"}``.
    """
    exclude = {e.upper() for e in (exclude or set())}
    candidates = [g for g in preferred if g.upper() not in exclude]

    if not candidates:
        return {"error": "no candidates after exclusions"}

    if not live:
        return {"gpu": candidates[0], "source": "fallback"}

    # Live mode: query qstat
    qstat_text = _run_qstat(ssh_host)
    if qstat_text is None:
        # qstat failed — fall back to static order
        return {"gpu": candidates[0], "source": "fallback", "warning": "qstat unavailable"}

    cfg = gpu_config or _DEFAULT_GPU_CONFIG
    agg = parse_qstat_f(qstat_text, gpu_config=cfg)
    result = score_gpus(
        agg,
        gpu_config=cfg,
        exclude=exclude,
        slots_needed=slots_needed,
        preferred_order=candidates,
    )
    return result


# ---------------------------------------------------------------------------
# Default GPU config (Hoffman2-style)
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

# Queue prefixes to always ignore
_EXCLUDED_PREFIXES: set[str] = {
    "gpu_P4",
    "gpu_k40",
    "gpu_smp",
    "gpu_test",
    "gpu_rh7",
    "gpu_a100_test",
    "gpu_l40s_multi",
}


# ---------------------------------------------------------------------------
# qstat parsing
# ---------------------------------------------------------------------------


def _run_qstat(ssh_host: str | None = None) -> str | None:
    """Run ``qstat -f -q gpu_*``, optionally over SSH. Returns stdout or None."""
    cmd = ["qstat", "-f", "-q", "gpu_*"]
    if ssh_host:
        cmd = ["ssh", "-o", "ConnectTimeout=10", ssh_host] + cmd
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def parse_qstat_f(
    text: str,
    gpu_config: dict[str, dict] | None = None,
) -> dict[str, dict]:
    """Parse ``qstat -f`` output into per-GPU-type aggregates.

    Returns ``{queue_prefix: {"used": int, "total": int, "active_nodes": int}}``.
    """
    cfg = gpu_config or _DEFAULT_GPU_CONFIG
    agg: dict[str, dict] = {}

    for line in text.strip().splitlines():
        line = line.strip()
        if not line or not line.startswith("gpu_"):
            continue

        parts = line.split()
        if len(parts) < 3:
            continue

        queue_host = parts[0]  # e.g. gpu_a100.q@g13
        slots_str = parts[2]  # e.g. 0/11/256

        is_disabled = len(parts) >= 6 and "d" in parts[-1]

        queue_name = queue_host.split(".q")[0]
        if queue_name in _EXCLUDED_PREFIXES:
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
    dict with "gpu" and scoring details, or "error" key.
    """
    cfg = gpu_config or _DEFAULT_GPU_CONFIG
    exclude = {e.upper() for e in (exclude or set())}

    scored: list[dict] = []
    for key, gpu_cfg in cfg.items():
        gpu_name = gpu_cfg["name"]
        if gpu_name.upper() in exclude:
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
                **extra,
            }
        )

    scored.sort(key=lambda x: x["score"], reverse=True)

    if scored:
        best = dict(scored[0])
        best["all_scores"] = scored
        best["source"] = "live"
        return best

    # Fallback: lowest utilization
    fallback: list[dict] = []
    for key, gpu_cfg in cfg.items():
        gpu_name = gpu_cfg["name"]
        if gpu_name.upper() in exclude:
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
                **extra,
            }
        )
    fallback.sort(key=lambda x: x["utilization"])

    if fallback:
        best_fb = dict(fallback[0])
        best_fb["all_scores"] = fallback
        best_fb["source"] = "live"
        best_fb["warning"] = "no GPU had enough free slots; scheduling may be delayed"
        return best_fb

    # Nothing from live data — use preferred order
    if preferred_order:
        return {
            "gpu": preferred_order[0],
            "source": "fallback",
            "warning": "no eligible GPU queues in qstat output",
        }

    return {"error": "no eligible GPU queues found", "all_scores": []}
