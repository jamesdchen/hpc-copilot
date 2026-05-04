"""Sweep the queue-wait predictor over future hours and rank windows.

Phase 3a: given a profile + cluster, evaluate
:func:`hpc_mapreduce.job.queue_wait_baseline.predict_queue_wait` at every
hour-of-week between now and ``now + within_hours``, then return the
``top_k`` windows sorted by ascending predicted wait.

The primitive is intended for the ``/submit-hpc`` slash command's
Step 4c smart planner: surface "if you wait until 06:00 the queue is
significantly emptier" without forcing the agent to enumerate
candidates itself.

Cold-start (predictor returns ``predicted_wait_sec=None``) windows are
omitted from the ranking — there's nothing useful to compare. If every
hour comes back cold, the result is an empty list and the slash command
falls back to "submit now".
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Literal

from claude_hpc._internal._time import utcnow
from hpc_mapreduce.job.queue_wait_baseline import predict_queue_wait

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["WindowCandidate", "best_submit_windows"]


Confidence = Literal["high", "medium", "low", "cold"]


@dataclass(frozen=True)
class WindowCandidate:
    """One candidate submit time and its predicted queue wait.

    ``submit_iso`` is in UTC, second-resolution. ``method`` is the
    queue-wait predictor's method — useful when surfacing the result
    so the caller can highlight blended_ma / global_ma fallbacks.
    """

    submit_iso: str
    predicted_wait_sec: int
    confidence: Confidence
    method: str
    n_bucket_samples: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def best_submit_windows(
    experiment_dir: Path,
    *,
    profile: str,
    cluster: str,
    within_hours: int = 24,
    top_k: int = 5,
) -> list[WindowCandidate]:
    """Sweep the predictor at hourly offsets and return the top_k.

    The sweep starts at the next hour boundary (rounding *now* up) so
    candidates are reproducible across consecutive calls. We sample at
    integer-hour offsets — hour-of-week buckets are 1h wide so
    sub-hour resolution wouldn't change the prediction.

    Returns up to *top_k* candidates sorted ascending by
    ``predicted_wait_sec``. Ties are broken by ascending ``submit_iso``
    so an earlier window wins when the predictor returns identical
    values.
    """
    if within_hours <= 0:
        return []
    if top_k <= 0:
        return []

    now = utcnow().replace(minute=0, second=0, microsecond=0)
    candidates: list[WindowCandidate] = []
    for h in range(1, int(within_hours) + 1):
        ts = now + timedelta(hours=h)
        iso = ts.isoformat(timespec="seconds")
        result = predict_queue_wait(
            experiment_dir,
            profile=profile,
            cluster=cluster,
            at_iso=iso,
        )
        if result.predicted_wait_sec is None:
            continue
        candidates.append(
            WindowCandidate(
                submit_iso=iso,
                predicted_wait_sec=int(result.predicted_wait_sec),
                confidence=result.confidence,
                method=result.method,
                n_bucket_samples=int(result.n_bucket_samples),
            )
        )

    candidates.sort(key=lambda c: (c.predicted_wait_sec, c.submit_iso))
    return candidates[: int(top_k)]
