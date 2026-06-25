"""Pins the layer-1 (run_id / journal) dedup decision in isolation.

``_resolve_layer1`` is the pure decision the submit path makes about an existing
journal record — historically inlined in ``submit_and_record`` and only
reachable through a full integration test. Extracting it lets each branch
(terminal-failure / in_flight / complete +/- drift +/- lever) be pinned without
touching the filesystem or the scheduler.
"""

from __future__ import annotations

from hpc_agent.ops.submit.runner import _DEDUP, _PROCEED, _resolve_layer1
from hpc_agent.state.run_record import RunRecord


def _record(*, status: str, executor: str = "", tasks_py_sha: str = "") -> RunRecord:
    return RunRecord(
        run_id="exp-abc12345",
        profile="cpu",
        cluster="c",
        ssh_target="user@host",
        remote_path="/scratch/exp",
        job_name="exp",
        job_ids=["1001"],
        total_tasks=4,
        submitted_at="2026-01-01T00:00:00Z",
        experiment_dir="/home/u/exp",
        status=status,
        executor=executor,
        tasks_py_sha=tasks_py_sha,
    )


def _decide(record, *, invalidate=False, executor=None, tasks_py_sha=None):
    return _resolve_layer1(
        record,
        invalidate_on_code_change=invalidate,
        current_executor=executor,
        current_tasks_py_sha=tasks_py_sha,
    )


def test_failed_record_proceeds():
    # #276: a terminal-failure corpse must not wedge future submits.
    d = _decide(_record(status="failed"))
    assert d.action == _PROCEED
    assert d.reason == "terminal_failure_resubmittable"


def test_abandoned_record_proceeds():
    d = _decide(_record(status="abandoned"))
    assert d.action == _PROCEED


def test_in_flight_record_dedups():
    d = _decide(_record(status="in_flight"))
    assert d.action == _DEDUP
    assert d.reason == "in_flight_blocks_duplicate"


def test_complete_no_drift_dedups():
    rec = _record(status="complete", executor="run a.py", tasks_py_sha="abc")
    d = _decide(rec, executor="run a.py", tasks_py_sha="abc")
    assert d.action == _DEDUP
    assert d.reason == "complete_idempotent_replay"
    assert d.warn_drift is False


def test_complete_drift_lever_off_warns_and_dedups():
    rec = _record(status="complete", executor="run OLD.py", tasks_py_sha="abc")
    d = _decide(rec, executor="run NEW.py", tasks_py_sha="abc")
    assert d.action == _DEDUP
    assert d.reason == "complete_code_drift_warned"
    assert d.warn_drift is True
    assert d.recorded_executor == "run OLD.py"  # threaded to the warning


def test_complete_drift_lever_on_proceeds_redo_in_place():
    rec = _record(status="complete", executor="run OLD.py", tasks_py_sha="abc")
    d = _decide(rec, invalidate=True, executor="run NEW.py", tasks_py_sha="abc")
    assert d.action == _PROCEED
    assert d.reason == "complete_code_drift_invalidated"


def test_complete_unprovable_drift_dedups():
    # Pre-#351 record never stamped an executor → cannot prove drift → dedup.
    rec = _record(status="complete", executor="", tasks_py_sha="")
    d = _decide(rec, executor="run NEW.py", tasks_py_sha="newsha")
    assert d.action == _DEDUP
    assert d.reason == "complete_idempotent_replay"
