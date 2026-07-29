"""``state/placement_drift`` — the third identity dimension's one predicate.

The load-bearing property is the CONSERVATIVE direction (run-queue plan
§10.S1): the leg must be purely additive on the pre-migration corpus, so
every absent/unusable value — on either side — must read as not-drifted.
The firing cases are membership over the recorded cluster set: a singleton
set is the run scope (equality), a list is the Phase-2 campaign
``placement_scope``.
"""

from __future__ import annotations

import pytest

from hpc_agent.state.placement_drift import (
    detect_placement_drift,
    normalize_recorded_placement,
)

# ── the firing cases ─────────────────────────────────────────────────────────


def test_current_outside_singleton_set_is_drift() -> None:
    drift = detect_placement_drift(recorded="hoffman2", current="carc")
    assert drift.changed is True
    assert drift.recorded == ("hoffman2",)
    assert drift.current == "carc"


def test_current_inside_singleton_set_is_not_drift() -> None:
    assert detect_placement_drift(recorded="hoffman2", current="hoffman2").changed is False


def test_membership_over_a_campaign_scope_list() -> None:
    """§10.S1.4: a campaign consent names a SET; membership, never equality."""
    scope = ["carc", "hoffman2"]
    assert detect_placement_drift(recorded=scope, current="carc").changed is False
    assert detect_placement_drift(recorded=scope, current="hoffman2").changed is False
    assert detect_placement_drift(recorded=scope, current="discovery").changed is True


# ── the conservative direction: absent/unusable disables, never fires ────────


@pytest.mark.parametrize(
    "recorded",
    [None, "", [], {"cluster": "carc"}, 42, ["carc", 42], ["carc", ""], [None]],
    ids=[
        "none",
        "empty-str",
        "empty-list",
        "mapping",
        "int",
        "mixed-list",
        "blank-member",
        "null-member",
    ],
)
def test_unusable_recorded_disables_the_check(recorded: object) -> None:
    """A recorded value we cannot read is a value we cannot prove drifted.

    The mixed-list cases are the sharp edge: keeping the valid SUBSET would
    shrink the set and make drift MORE likely to fire — the false-kill
    direction the rule forbids — so any unusable member disables the whole
    check.
    """
    drift = detect_placement_drift(recorded=recorded, current="carc")
    assert drift.changed is False
    assert drift.recorded is None


@pytest.mark.parametrize("current", [None, ""], ids=["none", "empty"])
def test_unknown_current_disables_the_check(current: str | None) -> None:
    """A pre-stamp sidecar (no ``cluster``) must never kill a consent."""
    drift = detect_placement_drift(recorded="hoffman2", current=current)
    assert drift.changed is False
    assert drift.recorded == ("hoffman2",)
    assert drift.current is None


def test_both_absent_is_not_drift() -> None:
    assert detect_placement_drift(recorded=None, current=None).changed is False


# ── the normalizer's contract ────────────────────────────────────────────────


def test_normalize_deduplicates_and_sorts() -> None:
    assert normalize_recorded_placement(["hoffman2", "carc", "hoffman2"]) == (
        "carc",
        "hoffman2",
    )


def test_normalize_str_is_a_singleton() -> None:
    assert normalize_recorded_placement("carc") == ("carc",)
