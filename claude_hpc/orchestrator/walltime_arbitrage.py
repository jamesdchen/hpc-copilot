"""Cold-start walltime arbitrage — survival under backfill contention.

When the smart-submit ``--test-only`` lattice probe has no recommendation
(no runtime priors to construct candidates), the user's nominal ask
collides with everyone else's nominal ask: 4h, 8h, 24h are the round
numbers every well-funded job requests. A campus user asking those
exact numbers competes for the same backfill slots higher-priority
jobs are reserving.

Asking for 3:45:00 instead of 4:00:00 fits in backfill shadows the
4:00:00 jobs don't reach — same compute budget, far less queue wait.
Lattice probing (when available) supersedes this; this is the
cold-start fallback that fires only when no priors exist to construct
a smarter recommendation.
"""

from __future__ import annotations

__all__ = ["arbitrage_walltime"]

# Configurable defaults. Wired through ``clusters.yaml`` at the planner
# boundary; the helper itself is pure and deterministic.
_FLOOR_SEC = 3600  # don't arbitrage asks below 1h
_OFFSET_SEC = 900  # subtract 15 minutes
_BOUNDARY_SEC = 300  # round down to nearest 5-min boundary


def arbitrage_walltime(walltime_sec: int) -> int:
    """Return the cold-start-fallback walltime ask for a nominal *walltime_sec*.

    Rules:

    - Below ``_FLOOR_SEC`` (1h): return *walltime_sec* unchanged. Short
      asks don't sit in backfill long enough for the trim to pay off,
      and pushing them lower risks cliff-killing the task.
    - Otherwise: subtract ``_OFFSET_SEC`` (15min), then round DOWN to
      ``_BOUNDARY_SEC`` (5min). Result is always strictly less than the
      input so the trimmed ask fits in backfill windows the round-number
      ask doesn't reach.

    Examples (all values in seconds)::

        arbitrage_walltime(3599)  == 3599   # below floor — unchanged
        arbitrage_walltime(3600)  == 2700   # 1h    -> 0:45
        arbitrage_walltime(14400) == 13500  # 4h    -> 3:45
        arbitrage_walltime(28800) == 27900  # 8h    -> 7:45
        arbitrage_walltime(86400) == 85500  # 24h   -> 23:45
    """
    if walltime_sec < _FLOOR_SEC:
        return walltime_sec
    return ((walltime_sec - _OFFSET_SEC) // _BOUNDARY_SEC) * _BOUNDARY_SEC
