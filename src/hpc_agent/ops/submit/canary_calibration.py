# @pure: no-io
"""Canary-calibrated array walltime — shrink the ceiling to the measured runtime.

The two-phase canary is a full real task whose wall-clock is MEASURED before the
main array launches. Before this kernel the array inherited whatever walltime the
planner (or a cold-start guess) picked at resolve time and carried it verbatim
through S2/S3 (``ops/submit/prepare_phase2_spec.py`` even makes "Phase 2 is
knowable with NO canary runtime state" an explicit invariant). Run
``causal_tune_tree_lgbm-7905102a`` is the failure: the cold-start planner had no
prior (``cold_start_prior_verb_unavailable``), the agent hand-picked a 6h
(21600s) walltime "for headroom", the canary then MEASURED ~10 min/task, and the
900-task array STILL submitted at the 6h ceiling — a 36× ``est_core_hours``
inflation (21600 vs ~600 realistic core-hours) that distorted the
overnight-consent budget conversation and padded the scheduler queue-priority ask.

This is the one place the measured canary wall-clock becomes a right-sized array
walltime:

    calibrated = min( requested , max( floor , ceil(canary_elapsed × factor) ) )

Two rules the arithmetic encodes:

* **Shrink-only — never exceed the approved ceiling.** ``requested`` is the
  walltime a human already greenlit at S2; calibration may only *tighten* it. The
  ``min(requested, …)`` clamp guarantees the array never asks for MORE than what
  was approved, even if the canary was slow or the ``floor`` is large.
* **A safety margin over the measured runtime**, so a task in the distribution's
  tail (a heavier grid point than the canary) is not killed at the ceiling:
  ``factor`` × the canary wall-clock, floored so a sub-second canary still buys a
  usable window.

Pure arithmetic + a disclosure string. Every consumer reads THIS definition (the
one-definition rule): the S2 brief recomputes ``est_core_hours`` off
``walltime_sec``, the S3 launch applies ``walltime_sec`` to the array spec, and
the S3 brief discloses ``disclosure`` — all three from the same call, so the
number the human consents to, the number the array requests, and the number the
brief shows can never diverge.

Stdlib only — a pure kernel testable without a scheduler.
"""

from __future__ import annotations

import dataclasses
import math

__all__ = [
    "DEFAULT_SAFETY_FACTOR",
    "DEFAULT_FLOOR_SEC",
    "WalltimeCalibration",
    "calibrate_array_walltime",
]

#: Multiplier over the measured canary wall-clock. 3× leaves headroom for a
#: heavier grid point than the (single) canary task without re-inflating toward
#: the cold-start ceiling. Disclosed in every calibration so a reviewer sees the
#: basis, not just the result.
DEFAULT_SAFETY_FACTOR = 3.0

#: Floor (seconds) for a calibrated walltime — 30 min. A canary that lands in
#: seconds must still buy a usable window (queue-wait jitter, a slow node, a
#: fatter task) rather than a walltime so tight the array is killed. The floor is
#: itself clamped by the approved ceiling: ``floor > requested`` never lifts the
#: ask above what the human greenlit.
DEFAULT_FLOOR_SEC = 1800


@dataclasses.dataclass(frozen=True)
class WalltimeCalibration:
    """The outcome of sizing an array walltime against a measured canary.

    Fields
    ------
    applied:
        True iff ``walltime_sec`` is STRICTLY below ``requested_walltime_sec`` —
        i.e. the calibration actually shrank the ask. False when there was
        nothing to shrink (no measurement, no request) or the measured runtime
        needs the whole approved ceiling. A consumer applies the shrunk value to
        the array spec ONLY when ``applied`` — otherwise the spec is untouched.
    walltime_sec:
        The walltime the array should request, in seconds. The calibrated value
        when ``applied``; otherwise the original ``requested_walltime_sec``
        (which may be ``None`` when the caller had no walltime at all).
    requested_walltime_sec:
        The walltime the caller/human approved — the ceiling calibration may
        never exceed. ``None`` when the spec carried no walltime.
    canary_elapsed_sec:
        The canary's measured wall-clock (seconds). ``None`` when no canary
        runtime was recorded (cache-skip path, an unreadable stamp).
    safety_factor:
        The multiplier applied to ``canary_elapsed_sec``.
    floor_sec:
        The minimum calibrated walltime before the approved-ceiling clamp.
    disclosure:
        A human-readable one-liner naming the shrunk value, factor, and canary
        basis — surfaced verbatim in the S2/S3 brief. ``None`` when calibration
        did not run (a measurement or a request was missing), so a brief that
        never calibrated stays byte-identical.
    """

    applied: bool
    walltime_sec: int | None
    requested_walltime_sec: int | None
    canary_elapsed_sec: int | None
    safety_factor: float
    floor_sec: int
    disclosure: str | None


def calibrate_array_walltime(
    *,
    canary_elapsed_sec: int | None,
    requested_walltime_sec: int | None,
    safety_factor: float = DEFAULT_SAFETY_FACTOR,
    floor_sec: int = DEFAULT_FLOOR_SEC,
) -> WalltimeCalibration:
    """Size an array walltime from a measured canary, shrink-only.

    ``calibrated = min(requested, max(floor, ceil(canary_elapsed × factor)))``.

    Returns a :class:`WalltimeCalibration`. When either input is missing or
    non-positive (a cache-skipped canary with no measurement, or a spec that
    carried no walltime) calibration is a NO-OP: ``applied=False``,
    ``walltime_sec=requested_walltime_sec`` (unchanged, possibly ``None``), and
    ``disclosure=None`` so a non-calibrating brief is byte-identical to before
    this kernel existed.

    ``safety_factor`` ≤ 0 or ``floor_sec`` < 0 fall back to the module defaults
    rather than producing a nonsensical (or ceiling-exceeding) value — a
    fat-fingered knob must never *loosen* an approved ceiling.
    """
    factor = safety_factor if safety_factor and safety_factor > 0 else DEFAULT_SAFETY_FACTOR
    floor = floor_sec if floor_sec is not None and floor_sec >= 0 else DEFAULT_FLOOR_SEC

    # Nothing to shrink: no approved ceiling, or no measurement. Carry the
    # request through untouched (the spec is left exactly as the caller built it).
    if requested_walltime_sec is None or requested_walltime_sec <= 0:
        return WalltimeCalibration(
            applied=False,
            walltime_sec=requested_walltime_sec,
            requested_walltime_sec=requested_walltime_sec,
            canary_elapsed_sec=canary_elapsed_sec if (canary_elapsed_sec or 0) > 0 else None,
            safety_factor=factor,
            floor_sec=floor,
            disclosure=None,
        )
    if canary_elapsed_sec is None or canary_elapsed_sec <= 0:
        return WalltimeCalibration(
            applied=False,
            walltime_sec=requested_walltime_sec,
            requested_walltime_sec=requested_walltime_sec,
            canary_elapsed_sec=None,
            safety_factor=factor,
            floor_sec=floor,
            disclosure=None,
        )

    padded = int(math.ceil(canary_elapsed_sec * factor))
    candidate = max(floor, padded)
    # Shrink-only: NEVER exceed the approved ceiling, even if the canary was slow
    # or the floor is large.
    calibrated = min(candidate, requested_walltime_sec)
    applied = calibrated < requested_walltime_sec

    if applied:
        disclosure = (
            f"array walltime calibrated {requested_walltime_sec}s → {calibrated}s "
            f"({factor:g}× the {canary_elapsed_sec}s canary, floor {floor}s), "
            f"never above the approved {requested_walltime_sec}s ceiling"
        )
    else:
        # Measured, but the canary × factor needs (at least) the whole approved
        # window — leave the ask at the ceiling and say why, so a reviewer knows
        # the walltime was checked against the canary, not merely unshrunk.
        disclosure = (
            f"array walltime held at the approved {requested_walltime_sec}s: "
            f"{factor:g}× the {canary_elapsed_sec}s canary (floor {floor}s) "
            f"meets or exceeds the ceiling, so there is nothing to shrink"
        )

    return WalltimeCalibration(
        applied=applied,
        walltime_sec=calibrated,
        requested_walltime_sec=requested_walltime_sec,
        canary_elapsed_sec=canary_elapsed_sec,
        safety_factor=factor,
        floor_sec=floor,
        disclosure=disclosure,
    )
