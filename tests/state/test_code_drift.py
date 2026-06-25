"""Pins the single executor/code drift predicate (`state.code_drift`).

This predicate was fixed twice (#351 sub-bug #5: once in `find_run_by_cmd_sha`,
then again at the layer-1 run_id gate) because it lived inline in two places.
It now has one home; these tests pin its rules, and
``test_layers_share_one_drift_predicate`` proves both dedup layers route through
this same function (so a future change to the rule cannot land in one and miss
the other).
"""

from __future__ import annotations

import inspect

import pytest

from hpc_agent.state.code_drift import detect_code_drift


def test_no_drift_when_identical():
    d = detect_code_drift(
        recorded_executor="run a.py",
        recorded_tasks_py_sha="abc",
        current_executor="run a.py",
        current_tasks_py_sha="abc",
    )
    assert d.drifted is False
    assert d.executor_changed is False and d.code_changed is False
    assert d.drifted_executor is None and d.drifted_tasks_py_sha is None


def test_executor_change_is_drift():
    d = detect_code_drift(
        recorded_executor="run OLD.py",
        recorded_tasks_py_sha="abc",
        current_executor="run NEW.py",
        current_tasks_py_sha="abc",
    )
    assert d.drifted is True
    assert d.executor_changed is True and d.code_changed is False
    assert d.drifted_executor == "run OLD.py"  # the RECORDED value, for the warning
    assert d.drifted_tasks_py_sha is None


def test_tasks_py_change_is_drift():
    d = detect_code_drift(
        recorded_executor="run a.py",
        recorded_tasks_py_sha="OLDsha",
        current_executor="run a.py",
        current_tasks_py_sha="NEWsha",
    )
    assert d.drifted is True
    assert d.code_changed is True and d.executor_changed is False
    assert d.drifted_tasks_py_sha == "OLDsha"


def test_both_dimensions_can_drift_together():
    d = detect_code_drift(
        recorded_executor="OLD",
        recorded_tasks_py_sha="OLDsha",
        current_executor="NEW",
        current_tasks_py_sha="NEWsha",
    )
    assert d.executor_changed is True and d.code_changed is True
    assert d.drifted is True


@pytest.mark.parametrize(
    ("recorded", "current"),
    [
        (None, "run a.py"),  # pre-#351 record never stamped an executor
        ("run a.py", None),  # caller did not supply a current executor
        ("", "run a.py"),  # empty recorded
        ("run a.py", ""),  # empty current
    ],
)
def test_absent_or_empty_value_is_never_drift(recorded, current):
    # The conservative rule: cannot prove a change without both sides present.
    d = detect_code_drift(
        recorded_executor=recorded,
        recorded_tasks_py_sha=None,
        current_executor=current,
        current_tasks_py_sha=None,
    )
    assert d.executor_changed is False
    assert d.drifted is False


def test_layers_share_one_drift_predicate():
    """Both dedup layers must call detect_code_drift — not re-implement it.

    Guards against the #351 fix-it-twice regression: a re-inlined predicate in
    either layer would silently diverge again.
    """
    from hpc_agent.ops.submit import runner
    from hpc_agent.state import runs

    layer1_src = inspect.getsource(runner._layer1_code_drift)
    layer2_src = inspect.getsource(runs.find_run_by_cmd_sha)
    assert "detect_code_drift" in layer1_src, "layer-1 must route through the shared predicate"
    assert "detect_code_drift" in layer2_src, "layer-2 must route through the shared predicate"
    # And neither layer re-inlines the raw comparison the shared predicate owns.
    for src in (layer1_src, layer2_src):
        assert "!= str(current_executor)" not in src
