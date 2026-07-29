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

__all__ = [
    "PlacementDrift",
    "detect_placement_drift",
    "normalize_recorded_placement",
    "placement_cluster_caps",
]


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

    Accepts the three shapes a consent's ``resolved`` dict may carry: one key
    (``"hoffman2"``, the run scope), a list of keys (the campaign
    ``placement_scope``), or a ``{cluster_key: {caps}}`` mapping (the Phase-2
    ``{cluster: cap}`` vocabulary, run-queue plan §3 — the KEY SET is the
    membership set; the values carry per-cluster caps and are
    :func:`placement_cluster_caps`'s business, not drift's). A mapping is usable
    only when every value is itself a mapping: the historical malformed shape
    ``{"cluster": "carc"}`` (a field name, not a cluster key) must keep reading
    as unusable, and requiring dict values is what distinguishes the cap
    vocabulary from it. No recorded consent predating the vocabulary can carry
    a mapping here — the write gate refused every mapping shape until the cap
    form was admitted — so the key-set reading never reinterprets old records.

    Anything else — empty, non-string members, non-dict mapping values —
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
    if isinstance(recorded, dict):
        # The {cluster: cap} form. Same any-unusable-member-disables rule as the
        # list: a shrunken key set is the false-kill direction.
        if not recorded or not all(
            isinstance(k, str) and k and isinstance(v, dict) for k, v in recorded.items()
        ):
            return None
        return tuple(sorted(recorded))
    return None


#: The two cap fields a ``{cluster: cap}`` placement value may carry. One home,
#: shared by the strict write gate (``ops/overnight._assert_placement_wellformed``)
#: and the tolerant consumption reader (:func:`placement_cluster_caps`), so the
#: two can never admit different vocabularies.
PLACEMENT_CAP_FIELDS = ("budget_cap", "walltime_cap")


def placement_cluster_caps(recorded: Any) -> dict[str, dict[str, float]]:
    """The per-cluster caps a ``{cluster: cap}`` placement declares, else ``{}``.

    The consumption-side reader of the Phase-2 vocabulary (run-queue plan §3):
    for the mapping form, each cluster's value may declare ``budget_cap`` /
    ``walltime_cap`` (positive finite numbers — the same rule as the consent's
    global caps). The str/list forms declare no caps. Tolerant in the
    consumption direction: a malformed cap value is DROPPED (contributes no
    cap) rather than raising — the strict shape refusal is the write gate's
    job, at record time, while the human is awake to fix it.
    """
    import math

    if normalize_recorded_placement(recorded) is None or not isinstance(recorded, dict):
        return {}
    out: dict[str, dict[str, float]] = {}
    for cluster, caps_raw in recorded.items():
        caps: dict[str, float] = {}
        for field in PLACEMENT_CAP_FIELDS:
            val = caps_raw.get(field)
            if (
                not isinstance(val, bool)
                and isinstance(val, (int, float))
                and math.isfinite(val)
                and val > 0
            ):
                caps[field] = float(val)
        out[cluster] = caps
    return out


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
