"""Tests for the ``walk-submit-ambiguities`` verb (Surface 2).

Pins the needs_resolution envelope shape and the partition invariants:

* the result is ``{resolved, ambiguities, provenance}``;
* REQUIRED_CALLER_FIELDS (goal / task_generator) are surfaced WITHOUT a
  safe_default — the resolution path cannot fabricate a sweep;
* AUTO_RESOLVABLE_FIELDS carry a real safe_default;
* resolve-resources is reused for walltime/gpu/partition/mpi_pe and those
  land in ``resolved`` (never ``ambiguities``).
"""

from __future__ import annotations

from typing import Any

import pytest

import hpc_agent.ops.walk_submit_ambiguities as mod
from hpc_agent._wire.queries.walk_submit_ambiguities import WalkSubmitAmbiguitiesInput
from hpc_agent.ops.walk_submit_ambiguities import walk_submit_ambiguities


@pytest.fixture(autouse=True)
def _stub_resolve_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub resolve-resources so the walk doesn't touch clusters.yaml / subprocess."""

    def fake(**kwargs: Any) -> dict[str, Any]:
        return {
            "walltime_sec": kwargs.get("walltime_sec") or 14400,
            "gpu_type": kwargs.get("gpu_type") or "a100",
            "partition": kwargs.get("partition"),
            "mpi_pe": kwargs.get("mpi_pe"),
            "provenance": {"walltime_sec": "stub", "gpu_type": "stub"},
        }

    monkeypatch.setattr(mod, "resolve_resources", fake)


def _walk(**overrides: Any) -> dict[str, Any]:
    spec = WalkSubmitAmbiguitiesInput.model_validate(overrides)
    result = walk_submit_ambiguities(spec=spec)
    return result.model_dump(mode="json")


def _amb_fields(out: dict[str, Any]) -> set[str]:
    return {a["field"] for a in out["ambiguities"]}


def _amb(out: dict[str, Any], field: str) -> dict[str, Any]:
    return next(a for a in out["ambiguities"] if a["field"] == field)


def test_envelope_shape() -> None:
    out = _walk(cluster="hoffman2", goal="g", tasks_py_present=True)
    assert set(out) == {"resolved", "ambiguities", "provenance"}
    assert isinstance(out["resolved"], dict)
    assert isinstance(out["ambiguities"], list)


def test_fully_resolved_caller_inputs_no_required_ambiguities() -> None:
    out = _walk(
        cluster="hoffman2",
        goal="train a forecaster",
        task_generator={"kind": "items_x_seeds", "params": {"seeds": [0, 1, 2]}},
        entry_point_resolved=True,
        data_axis_resolved=True,
        homogeneous_axes_resolved=True,
    )
    # cluster / goal / task_generator resolved.
    assert out["resolved"]["cluster"] == "hoffman2"
    assert out["resolved"]["goal"] == "train a forecaster"
    assert out["resolved"]["task_generator"]["kind"] == "items_x_seeds"
    # resources delegated + landed in resolved.
    assert out["resolved"]["walltime_sec"] == 14400
    assert out["resolved"]["gpu_type"] == "a100"
    # No goal/task_generator ambiguity.
    assert "goal" not in _amb_fields(out)
    assert "task_generator" not in _amb_fields(out)


def test_absent_goal_surfaced_without_safe_default() -> None:
    out = _walk(cluster="hoffman2", tasks_py_present=True)
    goal = _amb(out, "goal")
    assert goal["safe_default"] is None


def test_absent_task_generator_no_tasks_py_surfaced_without_default() -> None:
    out = _walk(cluster="hoffman2", goal="g")
    tg = _amb(out, "task_generator")
    # The incident-1b lock: no fabricated recipe in the safe_default slot.
    assert tg["safe_default"] is None


def test_absent_task_generator_with_tasks_py_is_not_ambiguity() -> None:
    """Hand-written tasks.py path — absence is sanctioned, not an ambiguity."""
    out = _walk(cluster="hoffman2", goal="g", tasks_py_present=True)
    assert "task_generator" not in _amb_fields(out)
    assert out["provenance"]["task_generator"] == "hand_written_tasks_py"


def test_cluster_multiple_configured_safe_default_lexicographic() -> None:
    out = _walk(configured_clusters=["zebra", "alpha", "mid"], goal="g", tasks_py_present=True)
    cl = _amb(out, "cluster")
    assert cl["candidates"] == ["zebra", "alpha", "mid"]
    assert cl["safe_default"] == "alpha"  # first lexicographically


def test_cluster_single_configured_auto_used() -> None:
    out = _walk(configured_clusters=["only"], goal="g", tasks_py_present=True)
    assert out["resolved"]["cluster"] == "only"
    assert "cluster" not in _amb_fields(out)


def test_entry_point_ambiguity_first_candidate_default() -> None:
    out = _walk(
        cluster="hoffman2",
        goal="g",
        tasks_py_present=True,
        entry_point_candidates=["train.py", "main.py"],
    )
    ep = _amb(out, "entry_point")
    assert ep["candidates"] == ["train.py", "main.py"]
    assert ep["safe_default"] == "train.py"


def test_data_axis_ambiguity_sequential_default_depends_on_entry_point() -> None:
    out = _walk(cluster="hoffman2", goal="g", tasks_py_present=True, entry_point_resolved=True)
    da = _amb(out, "data_axis")
    assert da["safe_default"] == {"kind": "sequential"}
    assert da["depends_on"] == ["entry_point"]


def test_uncovered_param_dict_safe_default_present_slot() -> None:
    out = _walk(
        cluster="hoffman2",
        goal="g",
        tasks_py_present=True,
        entry_point_resolved=True,
        data_axis_resolved=True,
        homogeneous_axes_resolved=True,
        uncovered_required_params=["samples", "epochs"],
        uncovered_param_defaults={"samples": 10000},
        executor_run_name="train",
    )
    up = _amb(out, "uncovered_param")
    # {param: <argparse default if any, else None>} — present slots, not absent.
    assert up["safe_default"] == {"samples": 10000, "epochs": None}
    assert up["context"]["executor"] == "train"
    assert up["context"]["required_no_default"] == ["samples", "epochs"]
    assert up["depends_on"] == ["entry_point"]


def test_resources_deferred_when_no_cluster() -> None:
    out = _walk(goal="g", tasks_py_present=True)  # no cluster, none configured
    assert "cluster" in _amb_fields(out)
    assert out["provenance"]["resources"] == "deferred_no_cluster"
    assert "walltime_sec" not in out["resolved"]


def test_data_axis_recommends_interview_hint_over_fail_safe(tmp_path) -> None:
    """Run-#12 finding 14: with the interview's materialized data_axis on disk,
    the walk recommends THAT (provenance interview_hint), not the sequential
    fail-safe — a y on the brief must not reclassify a declared BoundedHalo."""
    import json as _json

    hint = {"kind": "bounded_halo", "halo": {"expr": "halo"}}
    (tmp_path / "interview.json").write_text(
        _json.dumps({"_materialized": {"entry_point": {"data_axis": hint}}}),
        encoding="utf-8",
    )
    out = _walk(
        cluster="hoffman2",
        goal="g",
        tasks_py_present=True,
        entry_point_resolved=True,
        experiment_dir=str(tmp_path),
    )
    da = _amb(out, "data_axis")
    assert da["safe_default"] == hint
    assert out["provenance"]["data_axis"] == "interview_hint"
    assert "interview.json" in (da.get("context") or {}).get("source", "")


def test_data_axis_fail_safe_stands_without_a_hint(tmp_path) -> None:
    out = _walk(
        cluster="hoffman2",
        goal="g",
        tasks_py_present=True,
        entry_point_resolved=True,
        experiment_dir=str(tmp_path),
    )
    assert _amb(out, "data_axis")["safe_default"] == {"kind": "sequential"}
