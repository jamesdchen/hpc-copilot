"""Tests for the field→stage ownership facade (block-drive.md §4 routing).

``ops/field_ownership.py`` maps each workflow field to the block that RESOLVES
it, and turns a set of edited fields into the §4 advance / rerun /
advance_carrying routing decision. These cover the three routes for submit (the
family §4 specs concretely) plus the ownership-lookup policy.
"""

from __future__ import annotations

from hpc_agent.ops.field_ownership import OWNERSHIP, field_owner, route

# ── ownership lookup ──────────────────────────────────────────────────────────


def test_submit_resource_fields_owned_by_s1() -> None:
    assert field_owner("submit", "cluster") == "submit-s1"
    assert field_owner("submit", "gpu_type") == "submit-s1"
    assert field_owner("submit", "goal") == "submit-s1"
    assert field_owner("submit", "task_generator") == "submit-s1"


def test_submit_walltime_owned_downstream_by_s2() -> None:
    """The cost-cap field is first consumed by S2 (§4 'cap the cost' nudge)."""
    assert field_owner("submit", "walltime_sec") == "submit-s2"


def test_unknown_field_and_workflow_are_none() -> None:
    """Unknown field / workflow → None (treat as current-block, re-run to be safe)."""
    assert field_owner("submit", "no_such_field") is None
    assert field_owner("no_such_workflow", "cluster") is None


def test_ownership_covers_every_partition_field() -> None:
    """Every submit field enumerated in field_partition.py has an owner."""
    from hpc_agent.ops.submit.field_partition import (
        AUTO_RESOLVABLE_FIELDS,
        REQUIRED_CALLER_FIELDS,
    )

    for f in AUTO_RESOLVABLE_FIELDS | REQUIRED_CALLER_FIELDS:
        assert f in OWNERSHIP["submit"], f


# ── §4 routing: the three routes for submit ───────────────────────────────────


def test_route_unchanged_advances() -> None:
    """No changed fields → advance (a plain y)."""
    assert route("submit", "submit-s1", set(), "resolved") == "advance"


def test_route_current_block_field_reruns() -> None:
    """A changed field owned by the current block → rerun."""
    # cluster is owned by submit-s1; editing it at S1 re-runs S1.
    assert route("submit", "submit-s1", {"cluster"}, "resolved") == "rerun"


def test_route_downstream_field_advances_carrying() -> None:
    """A changed field owned strictly downstream → advance_carrying (§4 cap-cost)."""
    # walltime_sec is owned by S2; editing it at S1 carries the edit forward.
    assert route("submit", "submit-s1", {"walltime_sec"}, "resolved") == "advance_carrying"


def test_route_earlier_block_field_reruns_rewind() -> None:
    """A changed field owned by an EARLIER block → rerun (the rewind case)."""
    # cluster is owned by S1; editing it at S2 is a rewind → rerun.
    assert route("submit", "submit-s2", {"cluster"}, "canary_verified") == "rerun"


def test_route_mixed_current_and_downstream_reruns() -> None:
    """Any current/earlier-owned field forces rerun even if others are downstream."""
    assert route("submit", "submit-s1", {"cluster", "walltime_sec"}, "resolved") == "rerun"


def test_route_unowned_field_reruns_conservatively() -> None:
    """An unattributed changed field → rerun (conservative default)."""
    assert route("submit", "submit-s1", {"no_such_field"}, "resolved") == "rerun"


def test_route_all_downstream_multiple_advances_carrying() -> None:
    """Only downstream-owned edits → advance_carrying."""
    # At S1, walltime_sec (S2) is strictly downstream → carry.
    assert route("submit", "submit-s1", {"walltime_sec"}, "resolved") == "advance_carrying"
