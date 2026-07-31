"""Local-watchdog LIFECYCLE wiring: terminal teardown + the ``doctor`` sweep.

``infra/local_scheduler`` owns the rule (and is tested at
``tests/infra/test_local_scheduler.py``); this file pins that the rule is
actually REACHED — the half that was missing in the 2026-07-30 incident, where
the installer had an ``uninstall`` flag and no caller, so three tasks kept firing
every 15 minutes for days after their runs finished.

Two seats, and they are deliberately NOT the same seat twice:

* the guaranteed terminal harvest (``ops/monitor/harvest_guard``) — the terminal
  with the broadest reach (the poll loop's terminal branches and its
  abnormal-exit ``finally``, the reconcile settle arm, ``settle-run``), but NOT
  every terminal: the bulk / never-actuated closure paths
  (``reconcile_stale._close_record``, ``reconcile._never_actuated_abandon``,
  ``reconcile._safe_resubmit``, ``ops/supersession``) mark runs terminal without
  harvesting;
* ``doctor``'s stale-watchdog sweep — the BACKSTOP for exactly those paths, plus
  the tasks no terminal will ever fire for (installed by an older build, or
  orphaned by a deleted namespace).

No real scheduler is touched: the ``local_scheduler._run`` seam is injected.
Note the seam is now MARKER-GATED, so a namespace with no install marker never
reaches a subprocess at all — tests that mean to exercise the scheduler must call
``_mark_installed``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from xml.etree import ElementTree

import pytest

from hpc_agent._wire.actions.doctor_install import DoctorInstallSpec
from hpc_agent._wire.queries.doctor import DoctorSpec
from hpc_agent.infra import local_scheduler as ls
from hpc_agent.ops.monitor.harvest_guard import harvest_on_terminal
from hpc_agent.ops.recover.doctor import doctor
from hpc_agent.ops.recover.doctor_install import doctor_install
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


def _mark_installed(namespace: Path) -> Path:
    """Leave the durable install marker the scheduler gate keys on.

    Both the terminal teardown and the doctor sweep skip the scheduler entirely
    when no marker exists, so a test that means to exercise the scheduler path
    must look like a namespace where `doctor-install` really ran.
    """
    (Path(namespace) / "doctor.spec.json").write_text('{"notify": true}', encoding="utf-8")
    return Path(namespace)


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
    _mark_installed(journal_dir(exp))
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
    _mark_installed(journal_dir(exp))
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
    _mark_installed(journal_dir(exp))
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
    _mark_installed(journal_dir(exp))
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
# H1 — the incident regression, pinned END TO END through the real verb
# --------------------------------------------------------------------------- #
class _RecordingSchtasks(_FakeSchtasks):
    """Also accepts /Create, keeping the XML path it was handed."""

    def __init__(self) -> None:
        super().__init__([])
        self.xml_paths: list[Path] = []

    def __call__(self, argv: list[str], *, input_text: str | None = None, timeout: int):
        if argv[:2] == ["schtasks", "/Create"]:
            self.calls.append(argv)
            self.xml_paths.append(Path(argv[argv.index("/XML") + 1]))
            self.tasks = [*self.tasks, argv[argv.index("/TN") + 1]]
            return _cp(argv, rc=0)
        return super().__call__(argv, input_text=input_text, timeout=timeout)


def _registered_command(xml_path: Path) -> str:
    root = ElementTree.parse(xml_path).getroot()
    ns = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
    node = root.find(f"{ns}Actions/{ns}Exec/{ns}Command")
    assert node is not None
    return node.text or ""


def _fake_python_dir(tmp_path: Path, *, with_twin: bool) -> Path:
    scripts = tmp_path / "Scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "python.exe").write_text("", encoding="utf-8")
    if with_twin:
        (scripts / "pythonw.exe").write_text("", encoding="utf-8")
    return scripts


def test_installed_task_actually_runs_the_windowless_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """END TO END: what `doctor-install` REGISTERS is the windowless choice.

    The unit test for `windowless_interpreter` and the XML test that feeds a
    hardcoded pythonw path both pass even if `_scheduled_argv` reverts to
    `sys.executable` — which is precisely the incident (a console-subsystem
    python.exe flashing a window every 15 minutes). This pins the wiring: the
    `<Command>` in the registered XML must be the value the seam's chooser
    returned, not the interpreter that happens to be running.
    """
    exp = tmp_path / "exp"
    exp.mkdir()
    scripts = _fake_python_dir(tmp_path, with_twin=True)
    fake = _RecordingSchtasks()
    monkeypatch.setattr(ls, "platform_kind", lambda: "windows")
    monkeypatch.setattr(ls, "_run", fake)
    monkeypatch.setattr(ls.sys, "executable", str(scripts / "python.exe"))

    result = doctor_install(experiment_dir=exp, spec=DoctorInstallSpec())
    assert result.status == "installed"
    assert fake.xml_paths, "the install must go through /Create /XML"
    assert Path(_registered_command(fake.xml_paths[-1])).name.lower() == "pythonw.exe"
    # The echoed command the human reads must agree with what was registered.
    assert "pythonw.exe" in result.command


def test_installed_task_falls_back_to_python_when_no_twin_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback is the other half of the pin — and <Hidden> still carries it."""
    exp = tmp_path / "exp"
    exp.mkdir()
    scripts = _fake_python_dir(tmp_path, with_twin=False)
    fake = _RecordingSchtasks()
    monkeypatch.setattr(ls, "platform_kind", lambda: "windows")
    monkeypatch.setattr(ls, "_run", fake)
    monkeypatch.setattr(ls.sys, "executable", str(scripts / "python.exe"))

    doctor_install(experiment_dir=exp, spec=DoctorInstallSpec())
    xml_path = fake.xml_paths[-1]
    assert Path(_registered_command(xml_path)).name.lower() == "python.exe"
    root = ElementTree.parse(xml_path).getroot()
    ns = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
    hidden = root.find(f"{ns}Settings/{ns}Hidden")
    assert hidden is not None and hidden.text == "true"


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


def test_doctor_alerts_when_the_durable_spec_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The OTHER staleness signature (L2): the tick reads a spec that isn't there.

    A namespace cleaned up (or a journal home moved) out from under a registered
    task leaves it firing forever into nothing. The task XML marker is what keeps
    the sweep's gate open so the missing spec is REPORTED rather than skipped.
    """
    exp = tmp_path / "exp"
    exp.mkdir()
    _mark_installed(journal_dir(exp))
    namespace = _seed_foreign_namespace(tmp_path, "bbbbbbbbbbbb", statuses=["complete"])
    (namespace / "doctor.spec.json").unlink()
    ls.write_task_xml(namespace, "<Task/>")

    fake = _FakeSchtasks(["hpc-agent-doctor-bbbbbbbbbbbb"])
    monkeypatch.setattr(ls, "platform_kind", lambda: "windows")
    monkeypatch.setattr(ls, "_run", fake)

    out = doctor(experiment_dir=exp, spec=DoctorSpec(now="2026-07-30T00:00:00+00:00"))
    stale = [a for a in out["alerts"] if "stale local watchdog" in a["message"]]
    assert len(stale) == 1
    assert "doctor.spec.json" in stale[0]["message"]
    assert out["needs_attention"] is False


def test_doctor_sweep_backstops_the_closure_paths_that_never_harvest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M1: not every terminal runs the harvest — the sweep is what covers the rest.

    ``reconcile_stale._close_record``, ``reconcile._never_actuated_abandon``,
    ``reconcile._safe_resubmit`` and ``ops/supersession`` all mark runs terminal
    (``abandoned`` is in TERMINAL_STATUSES) WITHOUT calling
    ``harvest_on_terminal``, so the teardown never fires for them. Simulated here
    by closing the run through ``mark_run`` alone — no harvest — and asserting
    the watchdog is still registered but IS reported by the sweep.
    """
    from hpc_agent.state.journal import mark_run

    exp = tmp_path / "exp"
    exp.mkdir()
    namespace = _mark_installed(journal_dir(exp))
    upsert_run(exp, _record("orphaned", "in_flight"))
    (namespace / "repo.json").write_text(json.dumps({"experiment_dir": str(exp)}), encoding="utf-8")
    task = ls.task_name_for(repo_hash(exp))

    fake = _FakeSchtasks([task])
    monkeypatch.setattr(ls, "platform_kind", lambda: "windows")
    monkeypatch.setattr(ls, "_run", fake)

    # The bulk-closure shape: terminal status, no harvest, no teardown.
    mark_run(exp, "orphaned", status="abandoned")
    assert fake.deleted() == []
    assert fake.tasks == [task]

    out = doctor(experiment_dir=exp, spec=DoctorSpec(now="2026-07-30T00:00:00+00:00"))
    stale = [a for a in out["alerts"] if "stale local watchdog" in a["message"]]
    assert len(stale) == 1
    assert task in stale[0]["message"]


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
