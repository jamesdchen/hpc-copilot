"""Tests for the ``verify-submitted`` query primitive (#157).

It reads a run's job_ids from the journal, queries per-job scheduler state over
SSH, and flags error (SGE ``Eqw``) / held jobs — so the submit worker's Step 8b
is a verb call, not raw ``ssh qstat``.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from hpc_agent import errors
from hpc_agent.ops.verify_submitted import verify_submitted

# The sentinel-ack line a real scheduler query echoes (positive-evidence rule,
# docs/design/connection-broker.md). ``_cp`` appends it by default so fabricated
# "the query ran" stdouts pass the new ack gate; ``ack=None`` fabricates the
# silent/truncated channel (no ack) the ruling routes to UNKNOWN.
_ACK = "__HPC_SCHED_ACK__=0\n"


def _cp(stdout: str = "", rc: int = 0, ack: str | None = _ACK) -> subprocess.CompletedProcess[str]:
    body = stdout + ack if ack is not None else stdout
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=body, stderr="boom")


def _wire(
    monkeypatch,
    *,
    record,
    scheduler: str,
    ssh_stdout: str = "",
    ssh_rc: int = 0,
    ack: str | None = _ACK,
) -> None:
    monkeypatch.setattr("hpc_agent.state.journal.load_run", lambda *a, **k: record)
    monkeypatch.setattr(
        "hpc_agent.infra.clusters.load_clusters_config",
        lambda: {record.cluster: {"scheduler": scheduler}},
    )
    monkeypatch.setattr(
        "hpc_agent.infra.remote.ssh_run",
        lambda cmd, **k: _cp(stdout=ssh_stdout, rc=ssh_rc, ack=ack),
    )


def test_flags_sge_eqw_as_error(monkeypatch, tmp_path) -> None:
    record = SimpleNamespace(job_ids=["100", "101"], ssh_target="u@h", cluster="hoffman2")
    qstat = "job-ID prior name user state x\n----\n100 0.5 j u Eqw x\n101 0.5 j u r x\n"
    _wire(monkeypatch, record=record, scheduler="sge", ssh_stdout=qstat)
    out = verify_submitted(tmp_path, run_id="r1")
    assert out["ok"] is False
    assert out["error"] == ["100"]
    assert out["healthy"] == ["101"]
    assert out["states"] == {"100": "Eqw", "101": "r"}
    assert out["missing"] == []


def test_all_healthy_slurm_ok(monkeypatch, tmp_path) -> None:
    record = SimpleNamespace(job_ids=["7", "8"], ssh_target="u@h", cluster="disc")
    _wire(monkeypatch, record=record, scheduler="slurm", ssh_stdout="7 RUNNING\n8 PENDING\n")
    out = verify_submitted(tmp_path, run_id="r1")
    assert out["ok"] is True
    assert sorted(out["healthy"]) == ["7", "8"]
    assert out["error"] == [] and out["held"] == []


def test_missing_job_surfaced_not_errored(monkeypatch, tmp_path) -> None:
    record = SimpleNamespace(job_ids=["7", "9"], ssh_target="u@h", cluster="disc")
    # Only 7 is in the queue; 9 is absent → missing, not error.
    _wire(monkeypatch, record=record, scheduler="slurm", ssh_stdout="7 RUNNING\n")
    out = verify_submitted(tmp_path, run_id="r1")
    assert out["ok"] is True  # missing alone doesn't fail the gate
    assert out["missing"] == ["9"]
    assert out["healthy"] == ["7"]


def test_missing_record_raises_spec_invalid(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("hpc_agent.state.journal.load_run", lambda *a, **k: None)
    with pytest.raises(errors.SpecInvalid, match="no journal record"):
        verify_submitted(tmp_path, run_id="nope")


def test_no_job_ids_raises_spec_invalid(monkeypatch, tmp_path) -> None:
    record = SimpleNamespace(job_ids=[], ssh_target="u@h", cluster="disc")
    monkeypatch.setattr("hpc_agent.state.journal.load_run", lambda *a, **k: record)
    with pytest.raises(errors.SpecInvalid, match="no recorded job_ids"):
        verify_submitted(tmp_path, run_id="r1")


def test_ssh_transport_failure_raises(monkeypatch, tmp_path) -> None:
    record = SimpleNamespace(job_ids=["7"], ssh_target="u@h", cluster="disc")
    _wire(monkeypatch, record=record, scheduler="slurm", ssh_rc=255)
    with pytest.raises(errors.SshUnreachable):
        verify_submitted(tmp_path, run_id="r1")


def test_silent_ackless_read_raises_not_all_missing(monkeypatch, tmp_path) -> None:
    """Sentinel-ack ruling: an rc-0 empty read with NO ack token is a silently
    truncated / never-run channel — UNKNOWN, not "every submitted job already
    left the queue". Without the ack gate this stdout would report a
    freshly-landed array as entirely ``missing`` (never landed); it must raise.
    """
    record = SimpleNamespace(job_ids=["7", "8"], ssh_target="u@h", cluster="disc")
    _wire(monkeypatch, record=record, scheduler="slurm", ssh_stdout="", ack=None)
    with pytest.raises(errors.SshUnreachable):
        verify_submitted(tmp_path, run_id="r1")
