"""Tests for ``doctor-install`` + the scheduled-doctor OS notification (§5).

The install verb schedules the detection-only ``doctor`` scan on the OS
scheduler (schtasks / crontab) and is idempotent. The scheduled scan surfaces
stalls as an OS notification — never acts. No test mutates a real scheduler:
the scheduler seam (`doctor_install._run`) and the notifier are monkeypatched.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hpc_agent._wire.actions.doctor_install import DoctorInstallSpec
from hpc_agent._wire.queries.doctor import DoctorSpec
from hpc_agent.ops.recover import doctor_install as di
from hpc_agent.ops.recover import notify as notify_mod
from hpc_agent.ops.recover.doctor import doctor
from hpc_agent.ops.recover.doctor_install import doctor_install
from hpc_agent.state.journal import stamp_tick, upsert_run
from hpc_agent.state.run_record import RunRecord


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))


def _cp(argv: list[str], *, rc: int = 0, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=argv, returncode=rc, stdout=stdout, stderr="")


class _FakeSchtasks:
    """Stateful schtasks stand-in: /Query reflects prior /Create-/Delete."""

    def __init__(self) -> None:
        self.exists = False
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], *, input_text: str | None = None, timeout: int):
        self.calls.append(argv)
        head = argv[:2]
        if head == ["schtasks", "/Query"]:
            return _cp(argv, rc=0 if self.exists else 1)
        if head == ["schtasks", "/Create"]:
            self.exists = True
            return _cp(argv, rc=0)
        if head == ["schtasks", "/Delete"]:
            self.exists = False
            return _cp(argv, rc=0)
        raise AssertionError(f"unexpected argv {argv}")

    def created(self) -> bool:
        return any(a[:2] == ["schtasks", "/Create"] for a in self.calls)


class _FakeCrontab:
    """Stateful crontab stand-in over a single in-memory crontab body."""

    def __init__(self) -> None:
        self.content: str | None = None  # None => user has no crontab
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, argv: list[str], *, input_text: str | None = None, timeout: int):
        self.calls.append((argv, input_text))
        if argv == ["crontab", "-l"]:
            if self.content is None:
                return _cp(argv, rc=1)
            return _cp(argv, rc=0, stdout=self.content)
        if argv == ["crontab", "-"]:
            self.content = input_text
            return _cp(argv, rc=0)
        raise AssertionError(f"unexpected argv {argv}")


# --------------------------------------------------------------------------- #
# Windows / schtasks
# --------------------------------------------------------------------------- #
def test_windows_install_then_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeSchtasks()
    monkeypatch.setattr(di, "_platform", lambda: "windows")
    monkeypatch.setattr(di, "_run", fake)

    r1 = doctor_install(experiment_dir=tmp_path, spec=DoctorInstallSpec(interval_minutes=7))
    assert r1.status == "installed"
    assert r1.platform == "windows"
    assert r1.interval_minutes == 7
    assert r1.task_name.startswith("hpc-agent-doctor-")
    assert "doctor" in r1.command and "--spec" in r1.command
    assert fake.created()

    # Re-run identical params: no duplicate task, no second /Create.
    fake.calls.clear()
    r2 = doctor_install(experiment_dir=tmp_path, spec=DoctorInstallSpec(interval_minutes=7))
    assert r2.status == "already_installed"
    assert not fake.created()


def test_windows_uninstall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeSchtasks()
    monkeypatch.setattr(di, "_platform", lambda: "windows")
    monkeypatch.setattr(di, "_run", fake)

    doctor_install(experiment_dir=tmp_path, spec=DoctorInstallSpec())
    r_del = doctor_install(experiment_dir=tmp_path, spec=DoctorInstallSpec(uninstall=True))
    assert r_del.status == "uninstalled"
    # Removing an absent task is a no-op.
    r_again = doctor_install(experiment_dir=tmp_path, spec=DoctorInstallSpec(uninstall=True))
    assert r_again.status == "not_installed"


def test_install_failure_is_loud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hpc_agent import errors

    def boom(argv: list[str], *, input_text: str | None = None, timeout: int):
        if argv[:2] == ["schtasks", "/Query"]:
            return _cp(argv, rc=1)
        return _cp(argv, rc=1, stdout="access denied")

    monkeypatch.setattr(di, "_platform", lambda: "windows")
    monkeypatch.setattr(di, "_run", boom)
    with pytest.raises(errors.SpecInvalid):
        doctor_install(experiment_dir=tmp_path, spec=DoctorInstallSpec())


# --------------------------------------------------------------------------- #
# POSIX / crontab
# --------------------------------------------------------------------------- #
def test_posix_install_idempotent_and_uninstall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeCrontab()
    monkeypatch.setattr(di, "_platform", lambda: "posix")
    monkeypatch.setattr(di, "_run", fake)

    r1 = doctor_install(experiment_dir=tmp_path, spec=DoctorInstallSpec(interval_minutes=20))
    assert r1.status == "installed"
    assert r1.platform == "posix"
    assert fake.content is not None
    assert fake.content.count(r1.task_name) == 1
    assert fake.content.startswith("*/20 * * * *")

    # Idempotent: existing marker line → already_installed, no rewrite.
    n_writes = sum(1 for a, _ in fake.calls if a == ["crontab", "-"])
    r2 = doctor_install(experiment_dir=tmp_path, spec=DoctorInstallSpec(interval_minutes=20))
    assert r2.status == "already_installed"
    assert sum(1 for a, _ in fake.calls if a == ["crontab", "-"]) == n_writes
    assert fake.content.count(r1.task_name) == 1

    # Uninstall removes the marker line; re-uninstall is a no-op.
    r3 = doctor_install(experiment_dir=tmp_path, spec=DoctorInstallSpec(uninstall=True))
    assert r3.status == "uninstalled"
    assert r1.task_name not in (fake.content or "")
    r4 = doctor_install(experiment_dir=tmp_path, spec=DoctorInstallSpec(uninstall=True))
    assert r4.status == "not_installed"


def test_posix_install_preserves_other_cron_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeCrontab()
    fake.content = "0 3 * * * /usr/bin/backup\n"
    monkeypatch.setattr(di, "_platform", lambda: "posix")
    monkeypatch.setattr(di, "_run", fake)

    doctor_install(experiment_dir=tmp_path, spec=DoctorInstallSpec())
    assert "/usr/bin/backup" in (fake.content or "")


# --------------------------------------------------------------------------- #
# Durable spec
# --------------------------------------------------------------------------- #
def test_durable_spec_carries_notify_true(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeSchtasks()
    monkeypatch.setattr(di, "_platform", lambda: "windows")
    monkeypatch.setattr(di, "_run", fake)

    r = doctor_install(experiment_dir=tmp_path, spec=DoctorInstallSpec())
    assert r.notify is True
    spec_on_disk = json.loads(Path(r.spec_path).read_text(encoding="utf-8"))
    assert spec_on_disk == {"notify": True}
    # The scheduled command reads that durable spec non-interactively.
    assert r.spec_path in r.command


# --------------------------------------------------------------------------- #
# Scheduled-doctor notification (§5)
# --------------------------------------------------------------------------- #
def _record(run_id: str) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        profile="p",
        cluster="hoffman2",
        ssh_target="u@h",
        remote_path="/remote",
        job_name="j",
        job_ids=["100"],
        total_tasks=4,
        submitted_at="2026-07-03T00:00:00+00:00",
        experiment_dir="/exp",
        status="in_flight",
    )


def _make_stalled(tmp_path: Path) -> str:
    upsert_run(tmp_path, _record("stalled"))
    stamp_tick(
        "stalled",
        last_tick_at="2026-07-03T00:00:00+00:00",
        next_tick_due="2026-07-03T00:00:00+00:00",
        experiment_dir=tmp_path,
    )
    return "2026-07-03T01:00:00+00:00"


def test_doctor_notifies_when_stalled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = _make_stalled(tmp_path)
    seen: list[list[dict]] = []

    def _capture(proposals, *, experiment_dir):
        seen.append(proposals)
        return {"mechanism": "test"}

    monkeypatch.setattr(notify_mod, "raise_stall_notification", _capture)
    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now=now, notify=True))
    assert out["stalled_count"] == 1
    assert len(seen) == 1
    assert seen[0][0]["run_id"] == "stalled"


def test_doctor_default_does_not_notify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = _make_stalled(tmp_path)
    called = False

    def _spy(proposals, *, experiment_dir):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(notify_mod, "raise_stall_notification", _spy)
    # notify defaults False → in-session verb behavior unchanged.
    doctor(experiment_dir=tmp_path, spec=DoctorSpec(now=now))
    assert called is False


def test_doctor_no_notify_when_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def _spy(proposals, *, experiment_dir):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(notify_mod, "raise_stall_notification", _spy)
    spec = DoctorSpec(now="2026-07-03T01:00:00+00:00", notify=True)
    out = doctor(experiment_dir=tmp_path, spec=spec)
    assert out["stalled_count"] == 0
    assert called is False


# --------------------------------------------------------------------------- #
# Notifier mechanics
# --------------------------------------------------------------------------- #
def test_notify_falls_back_to_logfile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No platform notifier available → the loud log file is the floor.
    monkeypatch.setattr(notify_mod, "_try_run", lambda argv: False)
    monkeypatch.setattr(notify_mod.shutil, "which", lambda _: None)
    proposals = [{"run_id": "r1", "last_tick_at": "2026-07-03T00:00:00+00:00"}]
    rec = notify_mod.raise_stall_notification(proposals, experiment_dir=tmp_path)
    assert rec["mechanism"] == "logfile"
    assert rec["delivered"] is True
    log_text = Path(rec["log_path"]).read_text(encoding="utf-8")
    assert "r1" in log_text and "re-arm" in log_text


def test_notify_summary_counts_extras() -> None:
    proposals = [
        {"run_id": "a", "last_tick_at": "t"},
        {"run_id": "b", "last_tick_at": "t"},
        {"run_id": "c", "last_tick_at": "t"},
    ]
    text = notify_mod.summarize_proposals(proposals)
    assert "run a" in text
    assert "(+2 more" in text
