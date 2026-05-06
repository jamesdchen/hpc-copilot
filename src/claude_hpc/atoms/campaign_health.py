"""``campaign-health``: structured payload for campaign-level diagnostics.

Aggregates run-history signals (sidecars, runtime_prior samples, journal
records) into a single payload an LLM agent can consume to surface
patterns:

* "jobs hit walltime cliff on a100, recommend +30% walltime"
* "GPU underutilization on v100 — most runs finish in p50/3 of asked
  walltime, recommend right-sizing"
* "queue wait spiked Tue 8am — recommend off-peak submit"

claude-hpc is agent-driven: we don't call an LLM here. We emit a
structured payload PLUS a ``suggested_prompt`` string the calling agent
feeds verbatim to its model. That keeps the dependency footprint zero
and lets the harness pick its own model.

Idempotent: aggregates from on-disk state; no SSH, no scheduler calls.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from claude_hpc._internal._primitive import primitive

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["campaign_health"]


def _walltime_cliff_rate(samples: list[dict[str, Any]]) -> dict[str, float]:
    """Fraction of jobs whose elapsed_sec was >=95% of asked walltime.

    Buckets by ``gpu_type``. Empty buckets emit 0.0 so consumers can
    treat the dict as a dense mapping.
    """
    by_gpu: dict[str, list[float]] = {}
    for s in samples:
        gpu = str(s.get("gpu_type") or "cpu")
        elapsed = s.get("elapsed_sec")
        walltime = s.get("walltime_sec")
        if not isinstance(elapsed, (int, float)) or not isinstance(walltime, (int, float)):
            continue
        if walltime <= 0:
            continue
        ratio = float(elapsed) / float(walltime)
        by_gpu.setdefault(gpu, []).append(1.0 if ratio >= 0.95 else 0.0)
    return {gpu: (sum(vs) / len(vs)) for gpu, vs in by_gpu.items() if vs}


def _gpu_utilization(samples: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Bucket samples by gpu_type and compute n_runs + p50_elapsed_sec.

    Light: avoids depending on numpy. P50 = sorted middle value.
    """
    by_gpu: dict[str, list[int]] = {}
    for s in samples:
        gpu = str(s.get("gpu_type") or "cpu")
        elapsed = s.get("elapsed_sec")
        if isinstance(elapsed, (int, float)) and elapsed > 0:
            by_gpu.setdefault(gpu, []).append(int(elapsed))
    out: dict[str, dict[str, Any]] = {}
    for gpu, xs in by_gpu.items():
        xs_sorted = sorted(xs)
        p50 = xs_sorted[len(xs_sorted) // 2]
        out[gpu] = {"n_runs": len(xs), "p50_elapsed_sec": int(p50)}
    return out


def _failure_breakdown(samples: list[dict[str, Any]]) -> dict[str, int]:
    """Count failures by ``failure_category`` (unset → ``unknown``)."""
    counts: dict[str, int] = {}
    for s in samples:
        if int(s.get("exit_code", 0)) == 0:
            continue
        cat = str(s.get("failure_category") or "unknown")
        counts[cat] = counts.get(cat, 0) + 1
    return counts


def _build_prompt(payload: dict[str, Any]) -> str:
    """Render a ready-to-feed-LLM prompt summarizing the payload."""
    lines = [
        f"Here is a campaign health snapshot for {payload.get('campaign_id') or '(all runs)'}.",
        "",
        f"- {payload['n_runs']} runs total; {payload['n_complete']} complete, "
        f"{payload['n_failed']} failed.",
    ]
    cliff = payload.get("walltime_cliff_rate") or {}
    if cliff:
        parts = ", ".join(f"{gpu} {rate:.2f}" for gpu, rate in sorted(cliff.items()))
        lines.append(f"- GPU walltime-cliff rates: {parts}.")
    failure = payload.get("failure_breakdown") or {}
    if failure:
        parts = ", ".join(f"{cat} {n}" for cat, n in sorted(failure.items()))
        lines.append(f"- Failure breakdown: {parts}.")
    util = payload.get("gpu_utilization") or {}
    if util:
        parts = ", ".join(
            f"{gpu} (n={d['n_runs']}, p50={d['p50_elapsed_sec']}s)"
            for gpu, d in sorted(util.items())
        )
        lines.append(f"- GPU utilization: {parts}.")
    lines.append("")
    lines.append(
        "Identify the top 3 patterns to investigate and recommend specific "
        "tunings (walltime, mem, constraint)."
    )
    return "\n".join(lines)


@primitive(
    name="campaign-health",
    verb="query",
    side_effects=[],
    idempotent=True,
    cli="hpc-mapreduce campaign-health [--campaign-id <id>] [--since-iso <ts>]",
)
def campaign_health(
    experiment_dir: Path,
    *,
    campaign_id: str | None = None,
    since_iso: str | None = None,
    profile: str | None = None,
    cluster: str | None = None,
) -> dict[str, Any]:
    """Aggregate run-history signals into a structured health payload.

    *experiment_dir*: where the run sidecars and runtime_prior live.
    *campaign_id*: filter to a single campaign tag, or None for all runs.
    *since_iso*: filter samples submitted after this UTC ISO timestamp.
    *profile* + *cluster*: required when reading runtime_prior samples
    (which are bucketed per ``(profile, cluster)`` pair). When omitted,
    the function falls back to per-run sidecars only.

    Returns the payload pinned by ``schemas/campaign_health.output.json``.
    """
    from claude_hpc.state.runs import find_existing_runs, read_run_sidecar

    samples: list[dict[str, Any]] = []
    n_runs = 0
    n_complete = 0
    n_failed = 0

    if profile and cluster:
        try:
            from claude_hpc.state.runtime_prior import read_samples

            samples = read_samples(
                experiment_dir,
                profile=profile,
                cluster=cluster,
                only_successful=False,
            )
        except (FileNotFoundError, OSError, ValueError):
            samples = []

    # Walk per-run sidecars to count runs.
    for sidecar_path in find_existing_runs(experiment_dir):
        run_id = sidecar_path.stem
        try:
            sc = read_run_sidecar(experiment_dir, run_id)
        except (FileNotFoundError, OSError, ValueError):
            continue
        if campaign_id is not None and sc.get("campaign_id") != campaign_id:
            continue
        if since_iso is not None:
            submitted = sc.get("submitted_at")
            if isinstance(submitted, str) and submitted < since_iso:
                continue
        n_runs += 1
        status = (sc.get("status") or sc.get("lifecycle_state") or "").lower()
        if status == "complete":
            n_complete += 1
        elif status in ("failed", "abandoned", "timeout"):
            n_failed += 1

    if since_iso is not None:
        samples = [
            s
            for s in samples
            if isinstance(s.get("submitted_at"), str) and s["submitted_at"] >= since_iso
        ]
    if campaign_id is not None:
        samples = [s for s in samples if s.get("campaign_id") == campaign_id]

    payload: dict[str, Any] = {
        "campaign_id": campaign_id,
        "since_iso": since_iso,
        "n_runs": n_runs,
        "n_complete": n_complete,
        "n_failed": n_failed,
        "walltime_cliff_rate": _walltime_cliff_rate(samples),
        "queue_wait_quantiles": {},
        "failure_breakdown": _failure_breakdown(samples),
        "gpu_utilization": _gpu_utilization(samples),
    }
    payload["suggested_prompt"] = _build_prompt(payload)
    return payload
