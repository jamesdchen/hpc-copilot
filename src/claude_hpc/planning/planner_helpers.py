"""Pure helpers extracted from :mod:`planner` for navigability.

The main ``plan_submit`` orchestrator and the candidate-ranking core
stay in :mod:`planner`; the small standalone helpers here
(walltime formatting, ``sbatch --test-only`` ETA parsing, p_fail
hook, canary plan builder) have no in-module dependencies and pull
out cleanly.
"""

from __future__ import annotations

import re
from typing import Any

from claude_hpc._internal.time import parse_iso_utc, utcnow

__all__ = [
    "format_walltime_for_sbatch",
    "parse_test_only_eta",
    "p_fail_by_gpu_type",
    "build_canary_plan",
]


def format_walltime_for_sbatch(walltime_sec: int) -> str:
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


_TEST_ONLY_RE = re.compile(r"start at (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", re.IGNORECASE)


def parse_test_only_eta(text: str) -> int | None:
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
        ts = parse_iso_utc(m.group(1))
    except ValueError:
        return None
    delta = (ts - utcnow()).total_seconds()
    return max(0, int(delta))


def p_fail_by_gpu_type(snap: Any, gpu_types: list[str], scheduler: str) -> dict[str, float]:
    """Compute approximate per-GPU-type failure probability.

    Default implementation returns zeros; the production version would
    issue a windowed ``sacct`` query and bucket by AllocTRES gpu type.
    Surfacing this as a separate function keeps the integration pluggable
    and lets unit tests inject a deterministic value.
    """
    return {gpu: 0.0 for gpu in gpu_types}


def build_canary_plan(
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
