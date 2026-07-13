"""DECISION_POINTS registry — consistency with the workflow + op catalog."""

from __future__ import annotations

from typing import get_args

from hpc_agent._kernel.registry.operations import operations_catalog
from hpc_agent._wire.spawn_contract import (
    DECISION_POINTS,
    WORKFLOW_PROCEDURES,
    WorkflowName,
)


def test_workflow_name_matches_registry() -> None:
    """The delegatable-workflow ``Literal`` and the procedure registry are one set.

    The real invariant behind the ``WorkflowName`` comment: the type that
    constrains a :class:`SpawnRequest.workflow` field and the
    ``WORKFLOW_PROCEDURES`` key set must enumerate exactly the same workflows —
    a member added to one but not the other would let a spec name a workflow
    with no procedure (or vice versa). Both are ``{submit, status, aggregate,
    campaign}``. (Replaces the deleted-module citation
    ``test_spawn_prompt.test_workflow_name_matches_registry``.)
    """
    assert set(WORKFLOW_PROCEDURES) == set(get_args(WorkflowName))


def test_every_workflow_has_decision_points() -> None:
    assert set(DECISION_POINTS) == set(WORKFLOW_PROCEDURES)
    for workflow, points in DECISION_POINTS.items():
        assert points, workflow


def test_decision_point_ids_are_unique_per_workflow() -> None:
    for workflow, points in DECISION_POINTS.items():
        ids = [p.id for p in points]
        assert len(ids) == len(set(ids)), workflow


def test_code_points_name_a_real_primitive() -> None:
    catalog = {entry["name"] for entry in operations_catalog()}
    for workflow, points in DECISION_POINTS.items():
        for point in points:
            if point.decided_by == "code":
                assert point.primitive is not None, (workflow, point.id)
            if point.primitive is not None:
                assert point.primitive in catalog, (
                    f"{workflow}.{point.id} names primitive {point.primitive!r} "
                    "not in the operations catalog"
                )
