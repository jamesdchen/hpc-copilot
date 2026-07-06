"""Tests for the submit-input field partition (Surface 2, Phase 0).

The load-bearing assertion: the :class:`Ambiguity` guard FIRES — a
``safe_default`` on a REQUIRED_CALLER_FIELDS member raises at construction.
This is the incident-1b lock; a guard that can't fire is theater (see
docs/internals/engineering-principles.md).
"""

from __future__ import annotations

import pytest

from hpc_agent.ops.submit.field_partition import (
    AUTO_RESOLVABLE_FIELDS,
    REQUIRED_CALLER_FIELDS,
    Ambiguity,
    may_have_safe_default,
)


def test_partitions_are_disjoint() -> None:
    assert REQUIRED_CALLER_FIELDS.isdisjoint(AUTO_RESOLVABLE_FIELDS)


def test_required_caller_fields_membership() -> None:
    assert frozenset({"goal", "task_generator"}) == REQUIRED_CALLER_FIELDS


def test_may_have_safe_default_true_for_auto_resolvable() -> None:
    for field in AUTO_RESOLVABLE_FIELDS:
        assert may_have_safe_default(field) is True


def test_may_have_safe_default_false_for_required_caller() -> None:
    for field in REQUIRED_CALLER_FIELDS:
        assert may_have_safe_default(field) is False


def test_may_have_safe_default_false_for_unknown_field() -> None:
    # A name in neither set is not auto-resolvable by construction.
    assert may_have_safe_default("not_a_real_field") is False


@pytest.mark.parametrize("field", sorted(REQUIRED_CALLER_FIELDS))
def test_ambiguity_guard_fires_on_required_field_with_default(field: str) -> None:
    """THE fireable guard: a safe_default on goal/task_generator raises."""
    with pytest.raises(ValueError, match="REQUIRED_CALLER_FIELDS"):
        Ambiguity(field=field, safe_default="anything")


def test_ambiguity_guard_fires_on_task_generator_recipe() -> None:
    """The exact incident-1b shape: a fabricated task_generator recipe as a default."""
    recipe = {"kind": "items_x_seeds", "params": {"seeds": [0, 1, 2]}}
    with pytest.raises(ValueError, match="task_generator"):
        Ambiguity(field="task_generator", safe_default=recipe)


def test_ambiguity_required_field_without_default_is_allowed() -> None:
    """Absence (no safe_default) is the sanctioned shape for a required field."""
    amb = Ambiguity(field="task_generator", candidates=None)
    assert amb.safe_default is None
    assert amb.to_dict()["safe_default"] is None


def test_ambiguity_auto_resolvable_field_with_default_allowed() -> None:
    amb = Ambiguity(field="cluster", candidates=["a", "b"], safe_default="a")
    assert amb.safe_default == "a"


def test_ambiguity_uncovered_param_dict_default_allowed() -> None:
    """{param: None} is a PRESENT slot — allowed (uncovered_param is auto-resolvable)."""
    amb = Ambiguity(
        field="uncovered_param",
        candidates=["samples"],
        depends_on=("entry_point",),
        safe_default={"samples": None},
        context={"required_no_default": ["samples"]},
    )
    assert amb.safe_default == {"samples": None}


def test_ambiguity_falsy_default_on_auto_resolvable_allowed() -> None:
    """A falsy-but-present default (e.g. []) on an auto-resolvable field is real."""
    amb = Ambiguity(field="homogeneous_axes", safe_default=[])
    assert amb.safe_default == []


def test_to_dict_shape_matches_skill_entry() -> None:
    amb = Ambiguity(
        field="data_axis",
        candidates=None,
        depends_on=("entry_point",),
        safe_default={"kind": "sequential"},
    )
    d = amb.to_dict()
    assert d == {
        "field": "data_axis",
        "candidates": None,
        "depends_on": ["entry_point"],
        "safe_default": {"kind": "sequential"},
    }
    # context omitted when None.
    assert "context" not in d


def test_to_dict_includes_context_when_set() -> None:
    amb = Ambiguity(field="uncovered_param", safe_default={"samples": None}, context={"x": 1})
    assert amb.to_dict()["context"] == {"x": 1}


# ---------------------------------------------------------------------------
# CODE_DERIVED_FIELDS -- the third partition class (run #6 F1).
# ---------------------------------------------------------------------------


def test_code_derived_disjoint_from_other_classes() -> None:
    from hpc_agent.ops.submit.field_partition import (
        AUTO_RESOLVABLE_FIELDS,
        CODE_DERIVED_FIELDS,
        REQUIRED_CALLER_FIELDS,
    )

    assert not (CODE_DERIVED_FIELDS & REQUIRED_CALLER_FIELDS)
    assert not (CODE_DERIVED_FIELDS & AUTO_RESOLVABLE_FIELDS)


def test_executor_is_code_derived() -> None:
    from hpc_agent.ops.submit.field_partition import CODE_DERIVED_FIELDS

    assert "executor" in CODE_DERIVED_FIELDS
    assert "job_env" in CODE_DERIVED_FIELDS


def test_journal_unauthorable_is_code_derived_minus_sanctioned_echoes() -> None:
    from hpc_agent.ops.submit.field_partition import (
        CALLER_OVERRIDABLE_DERIVED_FIELDS,
        CODE_DERIVED_FIELDS,
        JOURNAL_UNAUTHORABLE_FIELDS,
    )

    # Unauthorable = code-derived MINUS the sanctioned input echoes (run_id /
    # cmd_sha / total_tasks) MINUS the caller-overridable-derived activation
    # fields (13-residual: a caller pin wins, so it is not refused here).
    expected = (
        CODE_DERIVED_FIELDS
        - {"run_id", "cmd_sha", "total_tasks"}
        - CALLER_OVERRIDABLE_DERIVED_FIELDS
    )
    assert expected == JOURNAL_UNAUTHORABLE_FIELDS
    assert "executor" in JOURNAL_UNAUTHORABLE_FIELDS
    assert "conda_env" not in JOURNAL_UNAUTHORABLE_FIELDS


def test_revise_resolved_binds_the_partition() -> None:
    """revise-resolved's derived set IS the partition's (bound, never copied) --
    the two can no longer drift (run #6 F1 promoted the list to the SoT)."""
    from hpc_agent.ops.revise_resolved import _DERIVED_FIELDS
    from hpc_agent.ops.submit.field_partition import CODE_DERIVED_FIELDS

    assert _DERIVED_FIELDS is CODE_DERIVED_FIELDS


def test_field_ownership_facade_re_exports_the_partition() -> None:
    from hpc_agent.ops import field_ownership
    from hpc_agent.ops.submit import field_partition

    assert field_ownership.CODE_DERIVED_FIELDS is field_partition.CODE_DERIVED_FIELDS
    assert (
        field_ownership.JOURNAL_UNAUTHORABLE_FIELDS is field_partition.JOURNAL_UNAUTHORABLE_FIELDS
    )
