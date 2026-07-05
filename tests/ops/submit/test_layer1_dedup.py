"""Pins the layer-1 (run_id / journal) dedup decision in isolation.

``_resolve_layer1`` is the pure decision the submit path makes about an existing
journal record — historically inlined in ``submit_and_record`` and only
reachable through a full integration test. Extracting it lets each branch
(terminal-failure / in_flight / complete +/- drift +/- lever) be pinned without
touching the filesystem or the scheduler.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.submit import SubmitSpec
from hpc_agent.ops.submit.runner import (
    _DEDUP,
    _PROCEED,
    _REFUSE,
    _resolve_layer1,
    submit_and_record,
)
from hpc_agent.state import run_record
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from pathlib import Path


def _record(
    *, status: str, executor: str = "", tasks_py_sha: str = "", cluster: str = "c"
) -> RunRecord:
    return RunRecord(
        run_id="exp-abc12345",
        profile="cpu",
        cluster=cluster,
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


def _decide(record, *, invalidate=False, executor=None, tasks_py_sha=None, cluster=None):
    return _resolve_layer1(
        record,
        invalidate_on_code_change=invalidate,
        current_executor=executor,
        current_tasks_py_sha=tasks_py_sha,
        current_cluster=cluster,
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


# ── proving run #5: cluster is not in run_id, so a cross-cluster retarget of a
#    LIVE run must refuse (never silently re-attach to the old cluster's canary).


def test_in_flight_cross_cluster_refuses():
    # (a) live on 'discovery', this submit targets 'hoffman2' → refuse loudly.
    rec = _record(status="in_flight", cluster="discovery")
    d = _decide(rec, cluster="hoffman2")
    assert d.action == _REFUSE
    assert d.reason == "in_flight_cluster_mismatch"


def test_in_flight_same_cluster_dedups():
    # (b) same cluster → the historical in_flight dedup, unchanged.
    rec = _record(status="in_flight", cluster="hoffman2")
    d = _decide(rec, cluster="hoffman2")
    assert d.action == _DEDUP
    assert d.reason == "in_flight_blocks_duplicate"


def test_in_flight_recorded_cluster_empty_dedups():
    # (c) an empty recorded cluster proves nothing — never refuse on absence
    #     (the "cannot prove it changed" precedent). Dedup unchanged.
    rec = _record(status="in_flight", cluster="")
    d = _decide(rec, cluster="hoffman2")
    assert d.action == _DEDUP
    assert d.reason == "in_flight_blocks_duplicate"


def test_failed_cross_cluster_proceeds():
    # (d) a terminal-failure corpse proceeds cross-cluster — redo-in-place is
    #     the legit recovery (#276); placement is irrelevant to a dead run.
    rec = _record(status="failed", cluster="discovery")
    d = _decide(rec, cluster="hoffman2")
    assert d.action == _PROCEED
    assert d.reason == "terminal_failure_resubmittable"


def test_complete_cross_cluster_dedups():
    # (e) a COMPLETE run dedups cross-cluster — the results already exist, so
    #     where they were produced is irrelevant to the replay.
    rec = _record(status="complete", cluster="discovery", executor="run a.py", tasks_py_sha="abc")
    d = _decide(rec, cluster="hoffman2", executor="run a.py", tasks_py_sha="abc")
    assert d.action == _DEDUP
    assert d.reason == "complete_idempotent_replay"


def _wire_spec(*, cluster: str) -> SubmitSpec:
    return SubmitSpec(
        profile="cpu",
        cluster=cluster,
        ssh_target="user@host",
        remote_path="/scratch/exp",
        job_name="exp",
        run_id="exp-abc12345",
        job_ids=["1001"],
        total_tasks=4,
    )


def test_submit_and_record_refuses_cross_cluster_retarget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a) integration: an in_flight run recorded on 'discovery' + a submit that
    targets 'hoffman2' raises SpecInvalid naming BOTH clusters — the caller
    never dedups this submit onto the other cluster's live run (proving run #5)."""
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", tmp_path / "home_hpc")
    experiment = tmp_path / "exp"
    experiment.mkdir()

    upsert_run(experiment, _record(status="in_flight", cluster="discovery"))

    with pytest.raises(errors.SpecInvalid) as ei:
        submit_and_record(experiment, spec=_wire_spec(cluster="hoffman2"))
    msg = str(ei.value)
    assert "discovery" in msg and "hoffman2" in msg
    assert "supersedes" in msg
