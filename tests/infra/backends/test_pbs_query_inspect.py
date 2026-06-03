"""PBS history query (qstat -xf -> Exit_status) + minimal inspect snapshot."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from hpc_agent.infra.backends import get_backend_class
from hpc_agent.infra.backends.query import _parse_qstat_full_pbs, query_pbs

# A realistic ``qstat -xf -t`` buffer: two finished subjobs (ok + failed),
# one still running, and the array parent (no index → skipped).
_QSTAT_XF = """Job Id: 12345[1].pbsserver
    Job_Name = cpu_array
    job_state = F
    Exit_status = 0
    resources_used.walltime = 01:00:00
    resources_used.ncpus = 4
Job Id: 12345[2].pbsserver
    Job_Name = cpu_array
    job_state = F
    Exit_status = 1
    resources_used.walltime = 00:00:12
    resources_used.ncpus = 4
Job Id: 12345[3].pbsserver
    Job_Name = cpu_array
    job_state = R
    resources_used.ncpus = 4
Job Id: 12345[].pbsserver
    Job_Name = cpu_array
    job_state = B
"""


def test_parse_qstat_full_pbs_states_and_exit_status():
    tasks: dict[int, dict] = {}
    _parse_qstat_full_pbs(_QSTAT_XF, tasks)

    assert tasks[1]["state"] == "COMPLETED" and tasks[1]["exit_code"] == "0"
    assert tasks[2]["state"] == "FAILED" and tasks[2]["exit_code"] == "1"
    assert tasks[3]["state"] == "RUNNING" and tasks[3]["exit_code"] is None
    # The array parent ``[]`` carries no task index → no entry.
    assert set(tasks) == {1, 2, 3}
    # Resource usage parsed: 01:00:00 -> 3600s, ncpus=4 -> cpu_s = 4*3600.
    assert tasks[1]["elapsed_s"] == 3600
    assert tasks[1]["cpu_s"] == 4 * 3600
    assert tasks[1]["job_id"] == "12345"


def test_query_pbs_pbspro_uses_x_flag():
    captured: list[list[str]] = []

    def _run(cmd, **kwargs):
        captured.append(cmd)
        return SimpleNamespace(returncode=0, stdout=_QSTAT_XF, stderr="")

    with patch("hpc_agent.infra.backends.query.subprocess.run", side_effect=_run):
        out = query_pbs(["12345"], fork="pbspro")

    assert captured[0][:4] == ["qstat", "-x", "-f", "-t"]  # pbspro needs -x for history
    assert out["tasks"][2]["state"] == "FAILED"
    assert out["errors"] == []


def test_query_pbs_torque_omits_x_flag():
    def _run(cmd, **kwargs):
        return SimpleNamespace(returncode=0, stdout=_QSTAT_XF, stderr="")

    with patch("hpc_agent.infra.backends.query.subprocess.run", side_effect=_run):
        captured: list[list[str]] = []

        def _run2(cmd, **kwargs):
            captured.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch("hpc_agent.infra.backends.query.subprocess.run", side_effect=_run2):
            query_pbs(["9"], fork="torque")
        assert captured[0][:3] == ["qstat", "-f", "-t"] and "-x" not in captured[0]


def test_engine_query_jobs_dispatches_to_pbs():
    def _run(cmd, **kwargs):
        return SimpleNamespace(returncode=0, stdout=_QSTAT_XF, stderr="")

    with patch("hpc_agent.infra.backends.query.subprocess.run", side_effect=_run):
        result = get_backend_class("pbspro").query_jobs(["12345"])
    assert result["tasks"][1]["state"] == "COMPLETED"


def test_pbs_inspect_returns_valid_minimal_snapshot():
    snap = get_backend_class("torque").inspect_cluster("mycluster", {})
    d = snap.to_dict()
    assert d["scheduler_kind"] == "torque"
    assert d["cluster"] == "mycluster"
    assert d["nodes"] == []  # node-level backfill data intentionally unpopulated
    assert any(e["code"] == "pbs_inspect_minimal" for e in d["errors"])
