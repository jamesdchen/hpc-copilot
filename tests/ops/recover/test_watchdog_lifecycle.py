"""Local-watchdog LIFECYCLE wiring: terminal teardown + the ``doctor`` sweep.

``infra/local_scheduler`` owns the rule (and is tested at
``tests/infra/test_local_scheduler.py``); this file pins that the rule is
actually REACHED — the half that was missing in the 2026-07-30 incident, where
the installer had an ``uninstall`` flag and no caller, so three tasks kept firing
every 15 minutes for days after their runs finished.

Two seats:

* the guaranteed terminal harvest (``ops/monitor/harvest_guard``), which every
  terminal path passes through (the poll loop's terminal branches and its
  abnormal-exit ``finally``, the reconcile settle arm, ``settle-run``);
* ``doctor``'s stale-watchdog probe, the sweep that catches a task no terminal
  will ever fire for (installed by an older build, or orphaned by a deleted
  namespace).

No real scheduler is touched: the ``local_scheduler._run`` seam is injected.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hpc_agent._wire.queries.doctor import DoctorSpec
from hpc_agent.infra import local_scheduler as ls
from hpc_agent.ops.monitor.harvest_guard import harvest_on_terminal
from hpc_agent.ops.recover.doctor import doctor
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord, journal_dir, repo_hash


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))


def _cp(argv: list[str], *, rc: int = 0, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=argv, returncode=rc, stdout=stdout, stderr="")


class _FakeSchtasks:
    def __init__(self, tasks: list[str] | None = None) -> None:
        self.tasks: list[str] = list(tasks or [])
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], *, input_text: str | None = None, timeout: int):
        self.calls.append(argv)
        head = argv[:2]
        if head == ["schtasks", "/Query"]:
            if "/FO" in argv:
                body = "".join(f'"\\{n}","N/A","Ready"\n' for n in self.tasks)
                return _cp(argv, rc=0, stdout=body)
            name = argv[argv.index("/TN") + 1]
            return _cp(argv, rc=0 if name in self.tasks else 1)
        if head == ["schtasks", "/Delete"]:
            name = argv[argv.index("/TN") + 1]
            self.tasks = [t for t in self.tasks if t != name]
            return _cp(argv, rc=0)
        raise AssertionError(f"unexpected argv {argv}")

    def deleted(self) -> list[str]:
        return [a[a.index("/TN") + 1] for a in self.calls if a[:2] == ["schtasks", "/Delete"]]


def _record(run_id: str, status: str) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        profile="p",
        cluster="hoffman2",
        ssh_target="u@h",
        remote_path="/remote",
        job_name="j",
        job_ids=["100"],
        total_tasks=1,
        submitted_at="2026-07-30T00:00:00+00:00",
        experiment_dir="/exp",
        status=status,
    )


def _no_op_harvest(experiment_dir: Path, run_id: str) -> object:
    """Stand in for ``aggregate-flow`` so the harvest never dials a cluster."""

    class _R:
        aggregated_metrics: dict[str, float] = {}
        escalation_reason = None
        combiner_dir_local = None

    return _R()


# --------------------------------------------------------------------------- #
# The terminal seat
# --------------------------------------------------------------------------- #
def test_terminal_harvest_removes_the_watchdog_when_nothing_is_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rule the whole class turns on: a finished run leaves no headless tick."""
    exp = tmp_path / "exp"
    exp.mkdir()
    journal_dir(exp)
    upsert_run(exp, _record("only", "complete"))
    task = ls.task_name_for(repo_hash(exp))

    fake = _FakeSchtasks([task])
    monkeypatch.setattr(ls, "platform_kind", lambda: "windows")
    monkeypatch.setattr(ls, "_run", fake)

    marker = harvest_on_terminal(
        exp,
        "only",
        terminal_cause="complete",
        record=_record("only", "complete"),
        _aggregate=_no_op_harvest,
        _sweep=lambda _d, _r: {},
    )
    assert marker["harvest_ok"] is True
    assert fake.deleted() == [task]
    assert fake.tasks == []


def test_terminal_harvest_keeps_the_watchdog_while_a_sibling_run_is_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-REPO watchdog: one run finishing must not blind the run still going."""
    exp = tmp_path / "exp"
    exp.mkdir()
    journal_dir(exp)
    upsert_run(exp, _record("done", "complete"))
    upsert_run(exp, _record("going", "in_flight"))
    task = ls.task_name_for(repo_hash(exp))

    fake = _FakeSchtasks([task])
    monkeypatch.setattr(ls, "platform_kind", lambda: "windows")
    monkeypatch.setattr(ls, "_run", fake)

    harvest_on_terminal(
        exp,
        "done",
        terminal_cause="complete",
        record=_record("done", "complete"),
        _aggregate=_no_op_harvest,
        _sweep=lambda _d, _r: {},
    )
    assert fake.deleted() == []
    assert fake.tasks == [task]


def test_terminal_harvest_survives_a_broken_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The teardown runs on a terminal path — it must never mask the terminal cause."""
    exp = tmp_path / "exp"
    exp.mkdir()
    journal_dir(exp)
    upsert_run(exp, _record("only", "complete"))

    def _boom(argv, *, input_text=None, timeout):  # noqa: ANN001, ANN202
        raise OSError("schtasks is on fire")

    monkeypatch.setattr(ls, "platform_kind", lambda: "windows")
    monkeypatch.setattr(ls, "_run", _boom)

    marker = harvest_on_terminal(
        exp,
        "only",
        terminal_cause="complete",
        record=_record("only", "complete"),
        _aggregate=_no_op_harvest,
        _sweep=lambda _d, _r: {},
    )
    assert marker["harvest_ok"] is True  # the harvest itself is unaffected


def test_abnormal_exit_on_a_live_run_does_not_remove_the_watchdog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The watch died, not the run: the dead-man's switch is exactly what is needed."""
    exp = tmp_path / "exp"
    exp.mkdir()
    journal_dir(exp)
    upsert_run(exp, _record("going", "in_flight"))
    task = ls.task_name_for(repo_hash(exp))

    fake = _FakeSchtasks([task])
    monkeypatch.setattr(ls, "platform_kind", lambda: "windows")
    monkeypatch.setattr(ls, "_run", fake)

    marker = harvest_on_terminal(
        exp,
        "going",
        terminal_cause="abnormal-exit",
        record=_record("going", "in_flight"),
        _aggregate=_no_op_harvest,
        _sweep=lambda _d, _r: {},
    )
    assert marker["harvest_skipped_reason"] == "run_not_terminal"
    assert fake.deleted() == []


# --------------------------------------------------------------------------- #
# The doctor sweep
# --------------------------------------------------------------------------- #
def _seed_foreign_namespace(tmp_path: Path, name: str, *, statuses: list[str]) -> Path:
    """A journaled namespace belonging to some OTHER experiment dir."""
    from hpc_agent.state.run_record import current_homedir

    namespace = current_homedir() / name
    (namespace / "runs").mkdir(parents=True, exist_ok=True)
    (namespace / "repo.json").write_text(
        json.dumps({"experiment_dir": str(tmp_path / name)}), encoding="utf-8"
    )
    (tmp_path / name).mkdir(exist_ok=True)
    (namespace / "doctor.spec.json").write_text('{"notify": true}', encoding="utf-8")
    for i, status in enumerate(statuses):
        (namespace / "runs" / f"r{i}.json").write_text(
            json.dumps({"run_id": f"r{i}", "status": status}), encoding="utf-8"
        )
    return namespace


def test_doctor_alerts_on_a_stale_watchdog_with_the_removal_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exp = tmp_path / "exp"
    exp.mkdir()
    journal_dir(exp)
    _seed_foreign_namespace(tmp_path, "aaaaaaaaaaaa", statuses=["complete"])

    fake = _FakeSchtasks(["hpc-agent-doctor-aaaaaaaaaaaa"])
    monkeypatch.setattr(ls, "platform_kind", lambda: "windows")
    monkeypatch.setattr(ls, "_run", fake)

    out = doctor(experiment_dir=exp, spec=DoctorSpec(now="2026-07-30T00:00:00+00:00"))
    stale = [a for a in out["alerts"] if "stale local watchdog" in a["message"]]
    assert len(stale) == 1
    assert "hpc-agent-doctor-aaaaaaaaaaaa" in stale[0]["message"]
    assert "doctor-install" in stale[0]["message"]
    assert "uninstall" in stale[0]["message"]
    # A stale watchdog is wasted ticks, not a stalled driver: it rides `alerts`
    # WITHOUT flipping the top-level verdict (the jsonschema/hook-probe contract).
    assert out["needs_attention"] is False
    # And the probe is read-only — doctor never removes what it reports.
    assert fake.deleted() == []


def test_doctor_is_silent_when_every_watchdog_is_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exp = tmp_path / "exp"
    exp.mkdir()
    journal_dir(exp)
    _seed_foreign_namespace(tmp_path, "cccccccccccc", statuses=["complete", "in_flight"])

    fake = _FakeSchtasks(["hpc-agent-doctor-cccccccccccc"])
    monkeypatch.setattr(ls, "platform_kind", lambda: "windows")
    monkeypatch.setattr(ls, "_run", fake)

    out = doctor(experiment_dir=exp, spec=DoctorSpec(now="2026-07-30T00:00:00+00:00"))
    assert [a for a in out["alerts"] if "stale local watchdog" in a["message"]] == []
