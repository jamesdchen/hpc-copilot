"""The single definition of PLACEMENT drift between a standing consent and a
consumption boundary — the third identity dimension.

``cmd_sha`` is pure PARAMETER identity (#207) and ``executor``/``tasks_py_sha``
are CODE identity (``state/code_drift.py``); neither says WHERE a run executes.
The run-queue design makes placement a machine decision
(``docs/plans/run-queue-placement-2026-07-28.md`` §10.S1), so a consent that
binds only those two dimensions would stay live across a re-placement to a
cluster the human never named — the S1 finding. This module is the placement
analog of ``code_drift``: a separate recorded dimension compared by a separate
predicate, deliberately NOT folded into ``cmd_sha`` (which would make the same
experiment on two clusters two different experiments and kill dedup,
reproduction targeting, and the shared-study merge).

The identity is the CLUSTER KEY (the ``clusters.yaml`` name the run sidecar
already stamps as ``cluster``), not a sha over the full placement block
``{cluster_key, ssh_target, remote_path, scheduler}``. Two reasons, recorded
here because §10.S1 sketched the sha form: (a) the failing leg's reason must be
legible in a park brief ("consent bound to hoffman2, run now places on carc") —
a sha hides which cluster; (b) ``remote_path`` legitimately varies WITHIN a
cluster once §10.S4's content-addressed trees land, and a consent must not die
because code was re-uploaded to the same cluster the human approved.

Comparison is MEMBERSHIP over a recorded cluster SET (§10.S1.4): a run-scope
consent records one key (a singleton set — membership degenerates to equality),
and a campaign-scope consent may record several (the Phase-2 ``placement_scope``
— a campaign spanning clusters is the point of §4, so equality would park on
every placement swing and defeat dynamic split). Same predicate, both scopes,
one home — ``code_drift``'s module docstring records what happens when a
predicate like this lives inline in two places.

Conservative in the same direction as :func:`code_drift.detect_code_drift`:
drift requires BOTH sides non-empty AND the current key outside the recorded
set. An absent value on either side disables the check — every pre-migration
consent predates the field, and firing on absence would kill every live consent
at upgrade.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["PlacementDrift", "detect_placement_drift", "normalize_recorded_placement"]


@dataclass(frozen=True)
class PlacementDrift:
    """Outcome of comparing a consent's recorded placement to the current one.

    ``recorded`` carries the normalized cluster-key set the consent bound (for
    the caller's refusal message) and is ``None`` when the check was disabled —
    so a caller can distinguish "did not drift" from "could not be checked"
    without re-deriving which.
    """

    changed: bool
    recorded: tuple[str, ...] | None
    current: str | None


def normalize_recorded_placement(recorded: Any) -> tuple[str, ...] | None:
    """The recorded placement as a sorted cluster-key tuple, or ``None`` if unusable.

    Accepts the two shapes a consent's ``resolved`` dict may carry: one key
    (``"hoffman2"``, the run scope) or a list of keys (the campaign
    ``placement_scope``). Anything else — empty, non-string members, a mapping —
    disables the check rather than guessing: an unreadable recorded value is a
    value we cannot prove drifted (the ``code_drift`` posture).
    """
    if isinstance(recorded, str):
        return (recorded,) if recorded else None
    if isinstance(recorded, list):
        # ANY unusable member disables the whole check rather than shrinking the
        # set to the valid members: a shrunken set is MORE likely to fire drift,
        # which is the false-kill direction the conservative rule forbids.
        if not recorded or not all(isinstance(k, str) and k for k in recorded):
            return None
        return tuple(sorted(set(recorded)))
    return None


def detect_placement_drift(*, recorded: Any, current: str | None) -> PlacementDrift:
    """Compare a consent's recorded placement to the boundary's current cluster.

    Drift fires only when BOTH sides are usable AND ``current`` is outside the
    recorded set — the symmetric absent-disables rule
    :func:`code_drift.detect_code_drift` states for the code dimensions, applied
    to the third. A consent with no recorded placement (every consent granted
    before the field existed) and a boundary with no known cluster (a sidecar
    predating the ``cluster`` stamp) both read as not-drifted, so this check is
    purely additive on the live corpus.
    """
    recorded_set = normalize_recorded_placement(recorded)
    if not recorded_set or not current:
        return PlacementDrift(changed=False, recorded=recorded_set, current=current or None)
    return PlacementDrift(
        changed=current not in recorded_set, recorded=recorded_set, current=current
    )
