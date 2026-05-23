"""Tests for the ``classify-axis`` primitive."""

from __future__ import annotations

import pytest

from hpc_agent import errors
from hpc_agent._schema_models.actions.classify_axis import ClassifyAxisInput
from hpc_agent.atoms.classify_axis import classify_axis
from hpc_agent.state.axes import read_axes, read_executor
from hpc_agent.incorporation.template import data_axis_from_config, plan_tasks


def _spec(**overrides: object) -> ClassifyAxisInput:
    base: dict[str, object] = {
        "run_name": "run",
        "run_signature_sha": "sig-abc",
        "data_axis": {"kind": "bounded_halo", "halo": {"expr": "train_window * 48"}},
    }
    base.update(overrides)
    return ClassifyAxisInput.model_validate(base)


def test_classify_axis_records_executor_entry(tmp_path) -> None:
    out = classify_axis(tmp_path, spec=_spec())
    assert out["wrote"] is True
    entry = read_executor(tmp_path, "run")
    assert entry is not None
    assert entry["run_signature_sha"] == "sig-abc"
    assert entry["data_axis"] == {"kind": "bounded_halo", "halo": {"expr": "train_window * 48"}}
    assert entry["classified_by"] == "interview"
    assert entry["classified_at"] == out["classified_at"]


def test_classify_axis_associative_defaults_monoid(tmp_path) -> None:
    classify_axis(tmp_path, spec=_spec(data_axis={"kind": "associative"}))
    entry = read_executor(tmp_path, "run")
    assert entry["data_axis"] == {"kind": "associative", "monoid": "moments"}


def test_classify_axis_preserves_scheduling_axes(tmp_path) -> None:
    """Recording a DataAxis must not clobber homogeneous_axes / axes."""
    from hpc_agent.state.axes import write_axes

    write_axes(
        tmp_path,
        axes=[{"name": "window", "size": 20}],
        homogeneous_axes=["window"],
    )
    classify_axis(tmp_path, spec=_spec())
    config = read_axes(tmp_path)
    assert config["homogeneous_axes"] == ["window"]
    assert config["axes"] == [{"name": "window", "size": 20}]
    assert "run" in config["executors"]


def test_classify_axis_multiple_runs_accumulate(tmp_path) -> None:
    classify_axis(tmp_path, spec=_spec(run_name="run_a"))
    classify_axis(tmp_path, spec=_spec(run_name="run_b", data_axis={"kind": "independent"}))
    config = read_axes(tmp_path)
    assert set(config["executors"]) == {"run_a", "run_b"}


def test_classify_axis_rejects_unsafe_halo_expr(tmp_path) -> None:
    with pytest.raises(errors.SpecInvalid):
        classify_axis(
            tmp_path,
            spec=_spec(data_axis={"kind": "bounded_halo", "halo": {"expr": "__import__('os')"}}),
        )


def test_classify_axis_idempotent(tmp_path) -> None:
    classify_axis(tmp_path, spec=_spec())
    first = read_executor(tmp_path, "run")
    classify_axis(tmp_path, spec=_spec())
    second = read_executor(tmp_path, "run")
    # Same classification → byte-equivalent modulo the timestamp.
    assert {k: v for k, v in first.items() if k != "classified_at"} == {
        k: v for k, v in second.items() if k != "classified_at"
    }


def test_classified_axis_feeds_plan_tasks(tmp_path) -> None:
    """Integration: classify BoundedHalo -> plan_tasks -> total()/resolve()."""
    classify_axis(tmp_path, spec=_spec())
    entry = read_executor(tmp_path, "run")
    axis = data_axis_from_config(entry["data_axis"])

    plan = plan_tasks([{"train_window": 30}], axis, chunks=4, series_length=8760)
    assert plan.total() == 4
    assert plan.axis_kind == "BoundedHalo"

    first = plan.resolve(0)
    assert first["start"] == 0 and first["halo"] == 0  # clamped at the series head
    second = plan.resolve(1)
    # halo request is train_window*48 = 1440, clamped to the chunk start.
    assert second["halo"] == min(1440, second["start"])
