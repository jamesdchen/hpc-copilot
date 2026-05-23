"""DECISION_POINTS registry — consistency with the workflow + op catalog."""

from __future__ import annotations

from hpc_agent._internal.operations import operations_catalog
from hpc_agent._schema_models.spawn_contract import DECISION_POINTS, WORKFLOW_PROCEDURES


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
