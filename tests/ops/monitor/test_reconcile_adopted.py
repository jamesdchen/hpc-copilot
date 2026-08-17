"""Adopted-run discrimination on the ``unable_to_verify`` seam.

Post-exploration checker: ``adopt-run`` adopts a run submitted OUTSIDE
hpc-agent — sidecar written post-hoc, journal record minted via submit-spec,
and NO hpc-agent runtime deployed with the tasks, so the cluster carries no
per-task status reporter, no announce markers, no jobmap marker. Reconcile's
reporter probe on such a run fails STRUCTURALLY, and the bare
``unable_to_verify`` verdict said nothing about why.

Pinned here:

* FIRES — an adopted run whose reporter probe fails (alive-check fine) stamps
  the discriminated cause ``unable_to_verify_cause ==
  "adopted_run_control_plane_absent"`` plus the composed disclosure note
  (per-task settle unavailable / alive-checks are the signal / settle-run
  directs terminal evidence).
* UNCHANGED — the alive-check-only signal path: an ALIVE-CHECK failure keeps
  the bare transport-fault reading even on an adopted run (no cause key), and
  a NON-adopted run's reporter failure keeps the historical bare message.
* NUMERICS/VERDICTS PRESERVED — the adopted marker never changes a settle
  outcome: a strict all-complete adopted run settles ``complete`` exactly as
  an un-adopted one does, with no cause key.

Cluster-free: the three SSH fan-out probes are monkeypatched (same doubles as
``test_reconcile_settle_liveness``); the package conftest stubs harvest +
announce.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import hpc_agent
from hpc_agent import errors
from hpc_agent.infra.io import append_jsonl_line
from hpc_agent.ops.monitor import reconcile as recon
from hpc_agent.ops.monitor.harvest_guard import harvest_marker_path
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord
from hpc_agent.state.runs import write_run_sidecar


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    return tmp_path


def _record(run_id: str, *, total_tasks: int = 2, job_ids=("100", "200")) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        profile="p",
        cluster="c",
        ssh_target="u@h",
        remote_path="/remote",
        job_name="j",
        job_ids=list(job_ids),
        total_tasks=total_tasks,
        submitted_at="2026-08-14T00:00:00Z",
        experiment_dir="/exp",
        status="in_flight",
    )


def _write_sidecar(experiment_dir: Path, run_id: str, *, extra: dict | None = None) -> None:
    write_run_sidecar(
        experiment_dir,
        run_id=run_id,
        cmd_sha="0" * 64,
        hpc_agent_version=hpc_agent.__version__,
        submitted_at="2026-08-14T00:00:00Z",
        executor="python3 run.py",
        result_dir_template="results/{task_id}",
        task_count=2,
        tasks_py_sha="1" * 64,
        extra=extra,
    )


def _stub_probes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    reporter: dict[str, int] | Exception,
    alive: set[str] | Exception,
) -> None:
    if isinstance(reporter, Exception):

        def _status(**_kw):
            raise reporter

        monkeypatch.setattr(recon, "_ssh_status_report", _status)
    else:
        monkeypatch.setattr(
            recon,
            "_ssh_status_report",
            lambda **_kw: {"summary": dict(reporter), "waves": {}},
        )
    monkeypatch.setattr(recon, "_ssh_list_combined_waves", lambda **_kw: [])
    if isinstance(alive, Exception):

        def _alive(**_kw):
            raise alive

        monkeypatch.setattr(recon, "_ssh_alive_job_ids", _alive)
    else:
        monkeypatch.setattr(recon, "_ssh_alive_job_ids", lambda **_kw: set(alive))


def _receipted_harvest(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def _fake(experiment_dir, run_id, *, terminal_cause, record=None, **_kw):
        calls.append(terminal_cause)
        append_jsonl_line(
            harvest_marker_path(experiment_dir, run_id),
            {"run_id": run_id, "terminal_cause": terminal_cause, "harvest_ok": True},
        )
        return {}

    monkeypatch.setattr(recon, "harvest_on_terminal", _fake)
    return calls


# ── (a) FIRES: adopted run + reporter failure → discriminated cause ──────────


def test_adopted_run_reporter_failure_stamps_discriminated_cause(tmp_path, monkeypatch):
    """The digest names WHY: per-task settle unavailable because the run was
    adopted post-hoc; alive-checks are the signal; settle-run directs terminal
    evidence. Journal status untouched (in_flight) — disclosure only."""
    _write_sidecar(tmp_path, "adopt_r1", extra={"adopted_by": "adopt-run"})
    upsert_run(tmp_path, _record("adopt_r1"))
    _stub_probes(
        monkeypatch,
        reporter=errors.RemoteCommandFailed("status reporter failed (rc=1): no reporter"),
        alive={"100", "200"},
    )

    result = recon.reconcile(tmp_path, "adopt_r1", scheduler="sge")

    assert result.status == "in_flight"  # journal untouched — could not verify
    last = result.last_status or {}
    assert last["verify_state"] == "unable_to_verify"
    assert last["unable_to_verify_cause"] == "adopted_run_control_plane_absent"
    note = last["unable_to_verify_note"]
    assert "adopted post-hoc" in note
    assert "per-task settle is unavailable" in note
    assert "alive-checks" in note or "alive-check" in note
    assert "settle-run" in note
    # The composed note names the run's OWN job_ids (composed, not authored).
    assert "100" in note and "200" in note
    # Envelope surface: lifecycle_state overridden to unable_to_verify, cause rides last_status.
    env = recon._reconcile_envelope(result)
    assert env["lifecycle_state"] == "unable_to_verify"
    assert env["last_status"]["unable_to_verify_cause"] == "adopted_run_control_plane_absent"


def test_adopted_run_nothing_alive_reporter_dead_still_discriminates(tmp_path, monkeypatch):
    """Nothing alive + reporter dead is not a provable verdict either way — the
    run stays unsettled (unchanged), but the adopted cause explains the stall."""
    _write_sidecar(tmp_path, "adopt_r2", extra={"adopted_by": "adopt-run"})
    upsert_run(tmp_path, _record("adopt_r2"))
    _stub_probes(
        monkeypatch,
        reporter=errors.RemoteCommandFailed("status reporter failed (rc=1): no reporter"),
        alive=set(),
    )

    result = recon.reconcile(tmp_path, "adopt_r2", scheduler="sge")

    assert result.status == "in_flight"  # settle gate requires a clean reporter
    last = result.last_status or {}
    assert last["verify_state"] == "unable_to_verify"
    assert last["unable_to_verify_cause"] == "adopted_run_control_plane_absent"


# ── (b) UNCHANGED: the alive-check-only signal path ──────────────────────────


def test_adopted_run_alive_check_failure_keeps_bare_transport_reading(tmp_path, monkeypatch):
    """An ALIVE-CHECK failure is a genuine connectivity fault even on an adopted
    run — the scheduler probe is the one signal an adopted run has. No cause key."""
    _write_sidecar(tmp_path, "adopt_r3", extra={"adopted_by": "adopt-run"})
    upsert_run(tmp_path, _record("adopt_r3"))
    _stub_probes(
        monkeypatch,
        reporter=errors.RemoteCommandFailed("status reporter failed (rc=1): no reporter"),
        alive=errors.RemoteCommandFailed("alive check failed (rc=255): connection reset"),
    )

    result = recon.reconcile(tmp_path, "adopt_r3", scheduler="sge")

    last = result.last_status or {}
    assert last["verify_state"] == "unable_to_verify"
    assert "unable_to_verify_cause" not in last
    assert "unable_to_verify_note" not in last


def test_non_adopted_run_reporter_failure_stays_bare(tmp_path, monkeypatch):
    """A normal (non-adopted) run keeps the historical bare unable_to_verify —
    no cause key, no note — on a reporter failure."""
    _write_sidecar(tmp_path, "plain_r4")  # no adoption marker
    upsert_run(tmp_path, _record("plain_r4"))
    _stub_probes(
        monkeypatch,
        reporter=errors.RemoteCommandFailed("status reporter failed (rc=1): boom"),
        alive={"100", "200"},
    )

    result = recon.reconcile(tmp_path, "plain_r4", scheduler="sge")

    last = result.last_status or {}
    assert last["verify_state"] == "unable_to_verify"
    assert "unable_to_verify_cause" not in last
    assert "unable_to_verify_note" not in last


def test_missing_sidecar_reads_as_not_adopted(tmp_path, monkeypatch):
    """No sidecar at all (``_sidecar = {}`` fallback) → NOT adopted; the bare
    historical reading holds."""
    upsert_run(tmp_path, _record("nosc_r5"))
    _stub_probes(
        monkeypatch,
        reporter=errors.RemoteCommandFailed("status reporter failed (rc=1): boom"),
        alive={"100", "200"},
    )

    result = recon.reconcile(tmp_path, "nosc_r5", scheduler="sge")

    last = result.last_status or {}
    assert last["verify_state"] == "unable_to_verify"
    assert "unable_to_verify_cause" not in last


# ── verdict preservation: the marker never changes a settle outcome ──────────


def test_adopted_run_strict_all_complete_settles_identically(tmp_path, monkeypatch):
    """An adopted run with a WORKING reporter and strict all-complete evidence
    settles ``complete`` byte-identically to an un-adopted run — the adoption
    marker is disclosure-only and never enters the verdict."""
    harvests = _receipted_harvest(monkeypatch)
    _write_sidecar(tmp_path, "adopt_r6", extra={"adopted_by": "adopt-run"})
    upsert_run(tmp_path, _record("adopt_r6", total_tasks=2))
    _stub_probes(
        monkeypatch,
        reporter={"complete": 2, "running": 0, "pending": 0, "failed": 0, "unknown": 0},
        alive=set(),
    )

    result = recon.reconcile(tmp_path, "adopt_r6", scheduler="sge")

    assert result.status == "complete"
    last = result.last_status or {}
    assert last["verdict_reason"] == "all_tasks_complete"
    assert "verify_state" not in last
    assert "unable_to_verify_cause" not in last
    assert harvests == ["complete"]


# ── the detection seam: every marker spelling, and only those ────────────────


@pytest.mark.parametrize(
    ("sidecar", "expected"),
    [
        ({"extra": {"adopted": True}}, True),
        ({"extra": {"adopted_by": "adopt-run"}}, True),
        ({"extra": {"provenance": "adopted"}}, True),
        ({"extra": {"provenance": "adopt-run"}}, True),
        ({}, False),
        # A top-level flag is NOT a marker: write_run_sidecar never persists
        # one, so the seam reads only the extra pocket (sidecar-field lint).
        ({"adopted": True}, False),
        ({"adopted": False}, False),
        ({"extra": {}}, False),
        ({"extra": {"provenance": "submit-flow"}}, False),
        ({"extra": "adopted"}, False),  # non-dict extra never trips the marker
    ],
    ids=[
        "extra_adopted",
        "extra_adopted_by",
        "extra_provenance_adopted",
        "extra_provenance_adopt_run",
        "empty",
        "top_level_flag_not_a_marker",
        "explicit_false",
        "empty_extra",
        "other_provenance",
        "non_dict_extra",
    ],
)
def test_is_adopted_run_marker_spellings(sidecar, expected):
    assert recon._is_adopted_run(sidecar) is expected
