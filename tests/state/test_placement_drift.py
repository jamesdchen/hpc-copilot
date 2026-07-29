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
    placement_cluster_caps,
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


# ── the {cluster: cap} form (run-queue plan §3, Phase 2) ─────────────────────


def test_cap_mapping_normalizes_to_its_key_set() -> None:
    """The {cluster: cap} form's KEY SET is the membership set — same drift
    semantics as the list form, the caps invisible to the predicate."""
    recorded = {"hoffman2": {"budget_cap": 10.0}, "carc": {}}
    assert normalize_recorded_placement(recorded) == ("carc", "hoffman2")
    assert detect_placement_drift(recorded=recorded, current="carc").changed is False
    assert detect_placement_drift(recorded=recorded, current="discovery").changed is True


def test_mapping_with_a_non_dict_value_stays_unusable() -> None:
    """The historical malformed shape ``{"cluster": "carc"}`` (a field name,
    not a cluster key) must keep disabling the check — requiring dict values
    is what separates the cap vocabulary from it."""
    assert normalize_recorded_placement({"cluster": "carc"}) is None
    assert normalize_recorded_placement({"carc": {}, "hoffman2": "oops"}) is None


def test_caps_extracted_from_the_mapping_form() -> None:
    caps = placement_cluster_caps(
        {"carc": {"budget_cap": 10, "walltime_cap": 3600.0}, "hoffman2": {}}
    )
    assert caps == {
        "carc": {"budget_cap": 10.0, "walltime_cap": 3600.0},
        "hoffman2": {},
    }


@pytest.mark.parametrize(
    "bad_cap",
    [0, -5, float("inf"), float("nan"), True, "10", None],
    ids=["zero", "negative", "inf", "nan", "bool", "str", "none"],
)
def test_unusable_cap_values_are_dropped_not_raised(bad_cap: object) -> None:
    """Consumption-side tolerance: a cap that cannot bind contributes NO cap
    (the strict refusal is the write gate's job, at record time)."""
    caps = placement_cluster_caps({"carc": {"budget_cap": bad_cap}})
    assert caps == {"carc": {}}


@pytest.mark.parametrize(
    "recorded",
    ["carc", ["carc", "hoffman2"], None, {"cluster": "carc"}, 42],
    ids=["str", "list", "none", "malformed-mapping", "int"],
)
def test_non_cap_forms_declare_no_caps(recorded: object) -> None:
    assert placement_cluster_caps(recorded) == {}
