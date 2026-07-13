"""Tests for the ``batch-status`` primitive (#2, connection-storm fix).

It enumerates the journal's in-flight runs, groups them by
``(ssh_target, scheduler)``, and issues ONE ``qstat -u $USER`` /
``squeue`` per group — distributing the parsed states back to each run
as ``TaskStatus`` values. The core invariant: N runs on one login node
cost ONE scheduler query per tick, not N.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from hpc_agent import errors
from hpc_agent.ops.monitor.batch_status import batch_status

# The sentinel-ack line a real scheduler query echoes (positive-evidence rule,
# docs/design/connection-broker.md). ``_cp`` appends it by default so fabricated
# "the query ran" stdouts pass the new ack gate; ``ack=None`` fabricates the
# silent/truncated channel (no ack) the ruling routes to UNKNOWN.
_ACK = "__HPC_SCHED_ACK__=0\n"


def _cp(stdout: str = "", rc: int = 0, ack: str | None = _ACK) -> subprocess.CompletedProcess[str]:
    body = stdout + ack if ack is not None else stdout
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=body, stderr="boom")


def _run(run_id: str, *, job_ids, ssh_target="u@h", cluster="disc", backend="slurm"):
    return SimpleNamespace(
        run_id=run_id,
        job_ids=job_ids,
        ssh_target=ssh_target,
        cluster=cluster,
        backend=backend,
    )


def _wire(monkeypatch, *, records, clusters, ssh_handler) -> None:
    monkeypatch.setattr("hpc_agent.state.index.find_in_flight_runs", lambda experiment_dir: records)
    monkeypatch.setattr("hpc_agent.infra.clusters.load_clusters_config", lambda: clusters)
    monkeypatch.setattr("hpc_agent.infra.remote.ssh_run", ssh_handler)
    # All backends require SSH unless a test overrides this.
    monkeypatch.setattr("hpc_agent.infra.backends.backend_requires_ssh", lambda name: True)


def test_one_query_for_two_runs_same_login_node(monkeypatch, tmp_path) -> None:
    """Two SLURM runs on the same login node => exactly ONE scheduler query."""
    records = [
        _run("r1", job_ids=["7", "8"]),
        _run("r2", job_ids=["9"]),
    ]
    calls: list[str] = []

    def _ssh(cmd, **k):
        calls.append(cmd)
        # squeue returns states for the union {7,8,9}.
        return _cp("7 RUNNING\n8 PENDING\n9 RUNNING\n")

    _wire(
        monkeypatch,
        records=records,
        clusters={"disc": {"scheduler": "slurm"}},
        ssh_handler=_ssh,
    )

    out = batch_status(experiment_dir=tmp_path)

    assert out["queries"] == 1, "two runs on one login node must collapse to one query"
    assert len(calls) == 1
    assert out["runs"]["r1"]["job_states"] == {"7": "running", "8": "pending"}
    assert out["runs"]["r2"]["job_states"] == {"9": "running"}
    assert out["runs"]["r1"]["missing_job_ids"] == []
    assert out["skipped"] == []


def test_missing_job_id_reported(monkeypatch, tmp_path) -> None:
    """A job id absent from the scheduler output lands in missing_job_ids."""
    records = [_run("r1", job_ids=["7", "9"])]

    _wire(
        monkeypatch,
        records=records,
        clusters={"disc": {"scheduler": "slurm"}},
        ssh_handler=lambda cmd, **k: _cp("7 RUNNING\n"),  # 9 absent
    )

    out = batch_status(experiment_dir=tmp_path)
    assert out["runs"]["r1"]["job_states"] == {"7": "running"}
    assert out["runs"]["r1"]["missing_job_ids"] == ["9"]


def test_distinct_login_nodes_get_separate_queries(monkeypatch, tmp_path) -> None:
    """Runs on different login nodes each cost their own query."""
    records = [
        _run("r1", job_ids=["7"], ssh_target="u@hostA"),
        _run("r2", job_ids=["8"], ssh_target="u@hostB"),
    ]
    calls: list[tuple[str, str]] = []

    def _ssh(cmd, *, ssh_target, **k):
        calls.append((ssh_target, cmd))
        return _cp("7 RUNNING\n" if ssh_target == "u@hostA" else "8 PENDING\n")

    _wire(
        monkeypatch,
        records=records,
        clusters={"disc": {"scheduler": "slurm"}},
        ssh_handler=_ssh,
    )

    out = batch_status(experiment_dir=tmp_path)
    assert out["queries"] == 2
    assert out["runs"]["r1"]["job_states"] == {"7": "running"}
    assert out["runs"]["r2"]["job_states"] == {"8": "pending"}


def test_unresolvable_scheduler_skipped(monkeypatch, tmp_path) -> None:
    """A run whose cluster has no scheduler in clusters.yaml is skipped, not crashed."""
    records = [_run("r1", job_ids=["7"], cluster="mystery")]
    _wire(
        monkeypatch,
        records=records,
        clusters={},  # no entry for "mystery"
        ssh_handler=lambda cmd, **k: _cp("7 RUNNING\n"),
    )
    out = batch_status(experiment_dir=tmp_path)
    assert out["queries"] == 0
    assert out["runs"] == {}
    assert out["skipped"] == [{"run_id": "r1", "reason": "unresolvable_scheduler"}]


def test_pure_api_backend_skipped(monkeypatch, tmp_path) -> None:
    """A pure-API backend (requires_ssh=False) is left to the per-run path."""
    records = [_run("r1", job_ids=["7"], backend="gha")]
    monkeypatch.setattr("hpc_agent.state.index.find_in_flight_runs", lambda experiment_dir: records)
    monkeypatch.setattr(
        "hpc_agent.infra.clusters.load_clusters_config",
        lambda: {"disc": {"scheduler": "slurm"}},
    )
    monkeypatch.setattr("hpc_agent.infra.backends.backend_requires_ssh", lambda name: False)
    out = batch_status(experiment_dir=tmp_path)
    assert out["queries"] == 0
    assert out["skipped"] == [{"run_id": "r1", "reason": "pure_api_backend"}]


def test_ssh_failure_raises(monkeypatch, tmp_path) -> None:
    """A non-zero SSH rc raises rather than silently zeroing the shared runs."""
    records = [_run("r1", job_ids=["7"])]
    _wire(
        monkeypatch,
        records=records,
        clusters={"disc": {"scheduler": "slurm"}},
        ssh_handler=lambda cmd, **k: _cp(rc=255),
    )
    with pytest.raises(errors.SshUnreachable):
        batch_status(experiment_dir=tmp_path)


def test_silent_ackless_read_raises_not_all_terminal(monkeypatch, tmp_path) -> None:
    """Sentinel-ack ruling: an rc-0 empty read with NO ack token is a silently
    truncated / never-run channel — UNKNOWN, not "every job left the queue".

    Without the ack gate this exact stdout (empty, rc 0) would report every
    job as ``missing_job_ids`` (terminal), flipping a fleet of live runs to
    terminal on one silent blip. The query must instead raise so the caller
    keeps the runs' prior state.
    """
    records = [_run("r1", job_ids=["7", "8"])]
    _wire(
        monkeypatch,
        records=records,
        clusters={"disc": {"scheduler": "slurm"}},
        ssh_handler=lambda cmd, **k: _cp("", ack=None),  # rc 0, no ack: silence
    )
    with pytest.raises(errors.SshUnreachable):
        batch_status(experiment_dir=tmp_path)


def test_no_in_flight_runs_is_empty(monkeypatch, tmp_path) -> None:
    """Zero in-flight runs => zero queries, empty result."""
    _wire(
        monkeypatch,
        records=[],
        clusters={"disc": {"scheduler": "slurm"}},
        ssh_handler=lambda cmd, **k: _cp(""),
    )
    out = batch_status(experiment_dir=tmp_path)
    assert out == {"runs": {}, "queries": 0, "skipped": []}
