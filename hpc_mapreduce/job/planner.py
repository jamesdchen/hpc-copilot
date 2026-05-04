"""Phase 4 planner: combine inspect, blacklist, and runtime priors.

This module emits the structured JSON the slash command hands to Claude
for cost-model judgment over candidate ``--constraint`` choices. It does
not pick a constraint itself — the value of the slash command is that a
human-aware reasoner can weigh per-node co-tenant context (long-running
heavy job ⇒ exclude; short jupyter session ⇒ allow) that no static
threshold captures cleanly.

Output shape (see top-level design ``docs/`` for the full contract)::

    {
      "profile": ..., "cluster": ..., "now_iso": ...,
      "candidates": [
        {
          "constraint": "<gpu-A>|<gpu-B>",
          "pool_size": 28,
          "healthy_nodes": ["..."],
          "stressed_nodes": [{"node": "...", "AllocMem_pct": 0.86, ...}],
          "blacklisted_nodes": [{"node": "...", "added_h_ago": 8, ...}],
          "eta_sec_via_test_only": 300,           # SLURM sbatch --test-only
          "runtime_prior_quantiles_sec": {"a100": {"p50": 4200, ...}, ...},
          "p_fail_30d": {"a100": 0.0, "v100": 0.14},
        },
        ...
      ],
      "needs_canary": false,
      "canary_plan": null,
    }
"""

from __future__ import annotations

__all__ = ["plan_submit"]

import re
import subprocess
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from hpc_mapreduce.infra.clusters import load_clusters_config
from hpc_mapreduce.infra.inspect import NodeSnapshot, inspect_cluster
from hpc_mapreduce.job.backfill import (
    BackfillProbe,
    ResourceTuple,
    build_lattice,
    cached_probe,
    pick_earliest,
    probe_lattice,
    recommend_walltime_sec,
)
from hpc_mapreduce.job.blacklist import get_active as get_active_blacklist
from hpc_mapreduce.job.runtime_prior import roll_up_quantiles


def plan_submit(
    experiment_dir: Path,
    *,
    profile: str,
    cluster: str,
    candidates: list[str] | None = None,
    cmd_sha: str | None = None,
    adversarial: bool = True,
    walltime_safety_mult: float = 1.30,
    walltime_ceiling_sec: int | None = None,
    base_mem_mb: int = 1024,
    base_cpus: int = 1,
) -> dict[str, Any]:
    """Score candidate constraints. Pure function over inputs + cluster snapshot.

    *candidates* is a list of constraint expressions. Each is either a
    single GPU type ("a100") or a SLURM-style alternation ("a40|a100").
    When *None*, defaults to ``[<each-type>] + [<all-types>]``: scoring
    both the strict and the wide pool.

    When *adversarial* is True (the default) the planner additionally
    right-sizes the walltime ask from runtime priors and probes a small
    ``(walltime × constraint)`` lattice via ``sbatch --test-only`` to
    find the tuple SLURM predicts will start earliest. Each candidate
    report gains ``backfill_probes`` and ``recommended_tuple`` fields.
    The pre-existing ``eta_sec_via_test_only`` field is left untouched
    so existing consumers are unaffected. *walltime_safety_mult* is the
    multiplier applied to the runtime prior's p95 (default 1.30 = 30%
    pad). Pass ``adversarial=False`` to disable the lattice probing
    entirely (useful for debugging or for clusters that throttle
    ``--test-only``).
    """
    clusters = load_clusters_config()
    if cluster not in clusters:
        raise KeyError(f"unknown cluster {cluster!r}; check clusters.yaml")
    cfg = clusters[cluster]
    scheduler = (cfg.get("scheduler") or "slurm").lower()
    gpu_types: list[str] = list(cfg.get("gpu_types") or [])

    if not candidates:
        candidates = list(gpu_types)
        if len(gpu_types) > 1:
            candidates.append("|".join(gpu_types))
    if not candidates:
        # No GPU types declared in clusters.yaml. Fall back to a single
        # CPU-only candidate so the report is non-empty.
        candidates = ["<cpu-only>"]

    # Snapshot the cluster (fully cached for 60s after first call).
    snap = inspect_cluster(cluster)

    # TTL-filtered blacklist for this cluster.
    bl_entries = get_active_blacklist(experiment_dir, cluster)
    bl_by_node: dict[str, dict[str, Any]] = {e["node"]: e for e in bl_entries}

    # Quantiles per GPU type (one rollup, shared across candidates).
    rollup = roll_up_quantiles(
        experiment_dir, profile=profile, cluster=cluster, cmd_sha=cmd_sha
    )
    quantiles = rollup["quantiles"]

    # Failure rates per GPU type (cluster-wide, last 30 days). Computed
    # lazily on first call; cluster query may fail and silently degrade.
    p_fail = _p_fail_by_gpu_type(snap, gpu_types, scheduler)

    candidate_reports: list[dict[str, Any]] = []
    for c in candidates:
        gpu_set = _gpu_types_in_constraint(c)
        pool = _nodes_for_constraint(snap.nodes, gpu_set)
        healthy: list[str] = []
        stressed: list[dict[str, Any]] = []
        blacklisted: list[dict[str, Any]] = []
        for n in pool:
            if n.name in bl_by_node:
                e = bl_by_node[n.name]
                blacklisted.append(_blacklist_summary(e))
                continue
            if n.is_stressed:
                stressed.append(_stressed_summary(n))
            elif not n.is_drained:
                healthy.append(n.name)
        # ETA via sbatch --test-only (SLURM only) — best effort.
        eta_sec = _eta_via_test_only(scheduler, c, cfg) if scheduler == "slurm" else None
        # Runtime prior quantiles for the GPU types in this constraint.
        c_quantiles = {gpu: quantiles[gpu] for gpu in gpu_set if gpu in quantiles}
        c_p_fail = {gpu: p_fail.get(gpu, 0.0) for gpu in gpu_set}
        report: dict[str, Any] = {
            "constraint": c,
            "pool_size": len(pool),
            "healthy_nodes": sorted(healthy),
            "stressed_nodes": stressed,
            "blacklisted_nodes": blacklisted,
            "eta_sec_via_test_only": eta_sec,
            "runtime_prior_quantiles_sec": c_quantiles,
            "p_fail_30d": c_p_fail,
        }
        if adversarial and scheduler == "slurm":
            report.update(
                _adversarial_report(
                    constraint=c,
                    gpu_set=gpu_set,
                    quantiles=quantiles,
                    cluster_cfg=cfg,
                    cluster_name=cluster,
                    safety_mult=walltime_safety_mult,
                    walltime_ceiling_sec=walltime_ceiling_sec,
                    base_mem_mb=base_mem_mb,
                    base_cpus=base_cpus,
                )
            )
        candidate_reports.append(report)

    needs_canary = bool(rollup.get("needs_canary"))
    canary_plan: dict[str, Any] | None = None
    if needs_canary:
        canary_plan = _build_canary_plan(candidate_reports, profile=profile, cluster=cluster)

    return {
        "profile": profile,
        "cluster": cluster,
        "now_iso": datetime.now(timezone.utc).isoformat(),
        "candidates": candidate_reports,
        "needs_canary": needs_canary,
        "canary_plan": canary_plan,
        "scheduler_kind": scheduler,
        "blacklist_active_count": len(bl_entries),
    }


def _gpu_types_in_constraint(c: str) -> list[str]:
    if not c or c == "<cpu-only>":
        return []
    return [t.strip() for t in c.split("|") if t.strip()]


def _nodes_for_constraint(
    nodes: list[NodeSnapshot], gpu_types: list[str]
) -> list[NodeSnapshot]:
    """Filter the node list to those that advertise any of *gpu_types*.

    Matching uses the ``Gres`` advertisement when present and the
    ``ActiveFeatures`` list as a fallback (some SLURM configurations
    expose the GPU type as a constraint feature, not a GRES type).

    Token-aware: ``a10`` will not match ``gpu:a100:2``. We split the
    GRES string on ``:`` and ``,`` and require an exact token match,
    since substring matching mis-classifies clusters that ship multiple
    GPU types whose names share prefixes.
    """
    if not gpu_types:
        return list(nodes)
    out: list[NodeSnapshot] = []
    for n in nodes:
        gres_tokens = {t.lower() for t in re.split(r"[:,\s]+", n.gres or "") if t}
        feature_tokens = {f.lower() for f in n.active_features}
        node_tokens = gres_tokens | feature_tokens
        if any(gpu.lower() in node_tokens for gpu in gpu_types):
            out.append(n)
    return out


def _blacklist_summary(entry: dict[str, Any]) -> dict[str, Any]:
    added = entry.get("added_at", "")
    try:
        ts = datetime.fromisoformat(added.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        added_h_ago = round((datetime.now(timezone.utc) - ts).total_seconds() / 3600.0, 1)
    except (ValueError, AttributeError):
        added_h_ago = None
    return {
        "node": entry.get("node"),
        "added_h_ago": added_h_ago,
        "expires_at": entry.get("expires_at"),
        "evidence_count": len(entry.get("evidence") or []),
    }


def _stressed_summary(n: NodeSnapshot) -> dict[str, Any]:
    return {
        "node": n.name,
        "AllocMem_pct": n.alloc_mem_pct,
        "CPULoad_frac": n.cpu_load_frac,
        "GresUsed": n.gres_used,
        "co_tenants": list(n.co_tenants),
    }


def _eta_via_test_only(scheduler: str, constraint: str, cluster_cfg: dict[str, Any]) -> int | None:
    """Predict queue ETA for a constraint via ``sbatch --test-only``.

    Returns seconds until the scheduler estimates the job would start,
    or ``None`` on any failure / non-SLURM scheduler. Best effort —
    the planner ignores ``None`` rather than refusing to score.

    Thin wrapper preserved for callers that only care about the
    constraint dimension. The adversarial path calls
    :func:`_eta_via_test_only_with_resources` directly.
    """
    eta, _ = _eta_via_test_only_with_resources(
        scheduler,
        cluster_cfg,
        constraint=constraint,
        walltime_sec=60,
        mem_mb=1024,
        cpus=1,
    )
    return eta


def _eta_via_test_only_with_resources(
    scheduler: str,
    cluster_cfg: dict[str, Any],
    *,
    constraint: str,
    walltime_sec: int,
    mem_mb: int,
    cpus: int,
) -> tuple[int | None, str]:
    """Probe the scheduler with a specific resource ask.

    Returns ``(eta_sec, raw_text)``. *raw_text* is the combined
    stdout/stderr of the probe so the caller can attach it to a debug
    field; we deliberately don't parse it further than the start-time
    regex. Any failure path yields ``(None, "")`` so the planner can
    silently skip that probe rather than abort the whole report.
    """
    if scheduler != "slurm":
        return None, ""
    host = cluster_cfg.get("host")
    user = cluster_cfg.get("user")
    if not host or not user:
        return None, ""
    try:
        from hpc_mapreduce.infra.remote import ssh_run
    except ImportError:
        return None, ""

    # --test-only never submits; it returns the scheduler's prediction.
    # We omit --array because the ETA only depends on the resource ask
    # for a single task, and the combination of --wrap and --array can
    # be rejected by some SLURM configurations.
    constraint_flag = (
        "" if constraint == "<cpu-only>" else f"--constraint={constraint!r}"
    )
    time_flag = _format_walltime_for_sbatch(walltime_sec)
    cmd = (
        f"sbatch --test-only --time={time_flag} --mem={int(mem_mb)}M "
        f"--cpus-per-task={int(cpus)} {constraint_flag} "
        "--wrap='true' 2>&1 || true"
    )
    try:
        cp = ssh_run(cmd, host=host, user=user, timeout=15)
    except (TimeoutError, subprocess.SubprocessError, FileNotFoundError, OSError):
        return None, ""
    text = (cp.stdout or "") + (cp.stderr or "")
    return _parse_test_only_eta(text), text


def _adversarial_report(
    *,
    constraint: str,
    gpu_set: list[str],
    quantiles: dict[str, dict[str, int]],
    cluster_cfg: dict[str, Any],
    cluster_name: str,
    safety_mult: float,
    walltime_ceiling_sec: int | None,
    base_mem_mb: int,
    base_cpus: int,
) -> dict[str, Any]:
    """Right-size walltime + probe lattice for a single candidate.

    Returns the dict slice to merge into the candidate report
    (``backfill_probes`` + ``recommended_tuple``). On any probe failure
    we still emit the right-sizing recommendation and rationale, so the
    slash command can surface "we'd ask 45m" even when the cluster
    doesn't honor ``--test-only``.
    """
    # Step 1: right-size walltime from priors.
    rec_wt, rec_rationale = recommend_walltime_sec(
        quantiles,
        gpu_set or [],
        safety_mult=safety_mult,
        ceiling_sec=walltime_ceiling_sec,
    )
    base = ResourceTuple(
        constraint=constraint, walltime_sec=rec_wt, mem_mb=base_mem_mb, cpus=base_cpus
    )
    # Step 2: build a 3-point lattice (1.0×, 1.5×, 2.0×) clamped to the ceiling.
    lattice = build_lattice(base, walltime_ceiling_sec=walltime_ceiling_sec)

    # Step 3: probe the lattice in parallel with cache wrapping.
    def _probe(t: ResourceTuple) -> BackfillProbe:
        eta, raw = _eta_via_test_only_with_resources(
            "slurm",
            cluster_cfg,
            constraint=t.constraint,
            walltime_sec=t.walltime_sec,
            mem_mb=t.mem_mb,
            cpus=t.cpus,
        )
        return BackfillProbe(tuple_=t, eta_sec=eta, raw_test_only=raw)

    probes = probe_lattice(lattice, cached_probe(cluster_name, _probe))
    pick = pick_earliest(probes)

    probes_out = [
        {
            "constraint": p.tuple_.constraint,
            "walltime_sec": p.tuple_.walltime_sec,
            "mem_mb": p.tuple_.mem_mb,
            "cpus": p.tuple_.cpus,
            "eta_sec": p.eta_sec,
        }
        for p in probes
    ]
    if pick is None:
        recommended: dict[str, Any] | None = {
            "constraint": base.constraint,
            "walltime_sec": base.walltime_sec,
            "mem_mb": base.mem_mb,
            "cpus": base.cpus,
            "predicted_eta_sec": None,
            "rationale": rec_rationale + "; no probe ETA available, using right-sized base",
        }
    else:
        recommended = {
            "constraint": pick.tuple_.constraint,
            "walltime_sec": pick.tuple_.walltime_sec,
            "mem_mb": pick.tuple_.mem_mb,
            "cpus": pick.tuple_.cpus,
            "predicted_eta_sec": pick.eta_sec,
            "rationale": rec_rationale,
        }
    return {
        "backfill_probes": probes_out,
        "recommended_tuple": recommended,
    }


def _format_walltime_for_sbatch(walltime_sec: int) -> str:
    """Format seconds as ``HH:MM:SS`` for sbatch ``--time``.

    SLURM accepts other formats (``MM``, ``MM:SS``, ``D-HH:MM:SS``); the
    canonical ``HH:MM:SS`` form is unambiguous and compact for any value
    under 100 hours, which is well above any realistic walltime ask.
    """
    secs = max(1, int(walltime_sec))
    h = secs // 3600
    m = (secs % 3600) // 60
    s = secs % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


_TEST_ONLY_RE = re.compile(
    r"start at (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", re.IGNORECASE
)


def _parse_test_only_eta(text: str) -> int | None:
    """Extract seconds-until-start from ``sbatch --test-only`` output.

    Output examples::

        sbatch: Job 12345 to start at 2026-01-01T18:30:00 using 1 ...
        sbatch: error: Batch job submission failed: ...

    Permissive: any unparseable input returns ``None``.
    """
    if not text:
        return None
    m = _TEST_ONLY_RE.search(text)
    if not m:
        return None
    try:
        ts = datetime.fromisoformat(m.group(1))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = (ts - datetime.now(timezone.utc)).total_seconds()
    return max(0, int(delta))


def _p_fail_by_gpu_type(
    snap: Any, gpu_types: list[str], scheduler: str
) -> dict[str, float]:
    """Compute approximate per-GPU-type failure probability.

    Default implementation returns zeros; the production version would
    issue a windowed ``sacct`` query and bucket by AllocTRES gpu type.
    Surfacing this as a separate function keeps the integration pluggable
    and lets unit tests inject a deterministic value.
    """
    return {gpu: 0.0 for gpu in gpu_types}


def _build_canary_plan(
    candidate_reports: list[dict[str, Any]], *, profile: str, cluster: str
) -> dict[str, Any]:
    """Return the lowest-ETA candidate as a 1-task canary plan.

    Ignores quality (no priors yet — that's why we're sending a canary).
    The slash command runs the canary, ingests the result into the
    runtime priors, then re-calls plan_submit which scores normally.
    """
    def _eta_key(r: dict[str, Any]) -> int:
        eta = r.get("eta_sec_via_test_only")
        # Sentinel for "ETA unknown" — sort to the back. Plain int so mypy
        # sees a concrete comparable type for the sorted() key.
        return int(eta) if isinstance(eta, (int, float)) else 10**9

    by_eta = sorted(candidate_reports, key=_eta_key)
    pick = by_eta[0] if by_eta else None
    if pick is None:
        return {
            "profile": profile,
            "cluster": cluster,
            "constraint": None,
            "task_count": 1,
            "note": "no candidates available; cannot canary",
        }
    return {
        "profile": profile,
        "cluster": cluster,
        "constraint": pick["constraint"],
        "task_count": 1,
        "rationale": (
            "No runtime priors exist for this (profile, cluster). Submit a "
            "1-task canary on the lowest-ETA candidate to seed the prior, "
            "then re-call plan-submit to score normally."
        ),
    }
