"""Tests for the ``watcher-install`` mutator + the cluster-side watcher script
(design §5 hybrid monitor).

The probe ladder is exercised with ``remote.ssh_run`` monkeypatched (a scripted
fake keyed on command substrings — no cluster is touched), asserting rung
selection, idempotent re-install, and the loud no-watcher envelope. The watcher
script is exercised for real, locally, by executing it with ``sys.executable``
against a tmp dir (it is stdlib-only). The last_read stamp + ALARM surfacing is
proven at the ``record_status`` seam.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.watcher_install import WatcherInstallResult, WatcherInstallSpec
from hpc_agent.ops.monitor import watcher_install as wi_mod
from hpc_agent.ops.monitor.watcher_install import watcher_install
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord

# Path to the shipped watcher script (executed for real in the script tests).
WATCHER_SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "hpc_agent"
    / "execution"
    / "mapreduce"
    / "templates"
    / "watcher"
    / "hpc_watcher.py"
)


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    return tmp_path


def _record(run_id: str = "r1") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        profile="p",
        cluster="c",
        ssh_target="u@h",
        remote_path="/remote/proj",
        job_name="jobx",
        job_ids=["100"],
        total_tasks=1,
        submitted_at="2026-07-03T00:00:00+00:00",
        experiment_dir="/exp",
        status="in_flight",
    )


class _ScriptedSSH:
    """A scripted ``ssh_run`` fake: rules match on command substrings.

    Each rule is ``(substring, (rc, stdout, stderr))``; the first match wins.
    Unmatched commands succeed with empty output. Every dispatched command is
    recorded for assertions.
    """

    def __init__(self, rules: list[tuple[str, tuple[int, str, str]]]) -> None:
        self.rules = rules
        self.sent: list[str] = []

    def __call__(
        self, cmd: str, *, ssh_target: str, **_kw: object
    ) -> subprocess.CompletedProcess[str]:
        self.sent.append(cmd)
        for needle, (rc, out, err) in self.rules:
            if needle in cmd:
                return subprocess.CompletedProcess(args=cmd, returncode=rc, stdout=out, stderr=err)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    def dispatched(self, needle: str) -> list[str]:
        return [c for c in self.sent if needle in c]


def _patch_ssh(monkeypatch: pytest.MonkeyPatch, ssh: _ScriptedSSH) -> None:
    monkeypatch.setattr(wi_mod, "ssh_run", ssh)


# ── probe-ladder branch selection ────────────────────────────────────────────


def test_crontab_viable_selects_cron(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    upsert_run(tmp_path, _record())
    ssh = _ScriptedSSH(
        [
            # crontab -l viable: "no crontab for user" is fine.
            ("crontab -l 2>&1", (1, "no crontab for user u", "")),
            ("command -v python3", (0, "/usr/bin/python3\n", "")),
        ]
    )
    _patch_ssh(monkeypatch, ssh)

    out = watcher_install(experiment_dir=tmp_path, run_id="r1", scheduler="slurm", action="install")

    assert out["mechanism"] == "cron"
    assert out["installed"] is True
    # The script was shipped and a crontab line registered with the run marker.
    assert ssh.dispatched("base64 -d > /remote/proj/.hpc/watcher/hpc_watcher.py")
    reg = ssh.dispatched("| crontab -")
    assert reg and "hpc-agent-watcher run_id=r1" in reg[0]
    assert "--run-dir /remote/proj" in reg[0]
    # scrontab is never probed once crontab took.
    assert not ssh.dispatched("scrontab")


def test_crontab_denied_slurm_falls_to_scrontab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upsert_run(tmp_path, _record())
    ssh = _ScriptedSSH(
        [
            # scrontab rule FIRST — "crontab -l" is a substring of "scrontab -l".
            ("scrontab -l 2>&1", (1, "no scrontab for user u", "")),
            ("crontab -l 2>&1", (1, "You (u) are not allowed to use this program (crontab)", "")),
            ("command -v python3", (0, "/usr/bin/python3\n", "")),
        ]
    )
    _patch_ssh(monkeypatch, ssh)

    out = watcher_install(experiment_dir=tmp_path, run_id="r1", scheduler="slurm")

    assert out["mechanism"] == "scrontab"
    assert out["installed"] is True
    reg = ssh.dispatched("| scrontab -")
    assert reg and "hpc-agent-watcher run_id=r1" in reg[0]
    assert "unavailable" in out["probes"]["crontab"]


def test_crontab_denied_non_slurm_falls_to_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upsert_run(tmp_path, _record())
    ssh = _ScriptedSSH(
        [
            ("crontab -l 2>&1", (127, "crontab: command not found", "")),
            ("command -v python3", (0, "/usr/bin/python3\n", "")),
        ]
    )
    _patch_ssh(monkeypatch, ssh)

    submitted: dict[str, object] = {}

    def _fake_submit(*, scheduler, ssh_target, remote_path, wrapper_path, job_name):
        submitted.update(scheduler=scheduler, wrapper_path=wrapper_path, job_name=job_name)
        return "98765"

    monkeypatch.setattr(wi_mod, "_submit_watcher_job", _fake_submit)

    out = watcher_install(experiment_dir=tmp_path, run_id="r1", scheduler="sge")

    assert out["mechanism"] == "job"
    assert out["installed"] is True
    assert out["detail"] == "job_id=98765"
    # SGE is not Slurm → scrontab skipped, not probed.
    assert "skipped" in out["probes"]["scrontab"]
    assert not ssh.dispatched("scrontab -l")
    # The wrapper was shipped and submitted through the seam.
    assert ssh.dispatched("base64 -d > /remote/proj/.hpc/watcher/hpc_watcher_job.sh")
    assert submitted["wrapper_path"] == "/remote/proj/.hpc/watcher/hpc_watcher_job.sh"


def test_nothing_available_is_loud_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    upsert_run(tmp_path, _record())
    ssh = _ScriptedSSH(
        [
            ("crontab -l 2>&1", (127, "crontab: command not found", "")),
            ("command -v python3", (0, "/usr/bin/python3\n", "")),
        ]
    )
    _patch_ssh(monkeypatch, ssh)
    # No submit binary available for this scheduler → job rung unavailable.
    monkeypatch.setattr(wi_mod, "_resolve_submit_bin", lambda scheduler: None)

    out = watcher_install(experiment_dir=tmp_path, run_id="r1", scheduler="sge")

    assert out["installed"] is False
    assert out["mechanism"] == "none"
    assert "OVERNIGHT BLINDNESS PERSISTS" in out["reason"]


def test_job_submit_failure_falls_to_loud_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upsert_run(tmp_path, _record())
    ssh = _ScriptedSSH(
        [
            ("crontab -l 2>&1", (127, "crontab: command not found", "")),
            ("command -v python3", (0, "/usr/bin/python3\n", "")),
        ]
    )
    _patch_ssh(monkeypatch, ssh)

    def _boom(**_kw: object) -> str:
        raise RuntimeError("sbatch exploded")

    monkeypatch.setattr(wi_mod, "_submit_watcher_job", _boom)

    out = watcher_install(experiment_dir=tmp_path, run_id="r1", scheduler="sge")

    assert out["installed"] is False
    assert out["mechanism"] == "none"
    assert "sbatch exploded" in out["probes"]["job"]


# ── idempotent re-install ────────────────────────────────────────────────────


def test_reinstall_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    upsert_run(tmp_path, _record())
    ssh = _ScriptedSSH(
        [
            ("crontab -l 2>&1", (0, "", "")),
            ("command -v python3", (0, "/usr/bin/python3\n", "")),
        ]
    )
    _patch_ssh(monkeypatch, ssh)

    watcher_install(experiment_dir=tmp_path, run_id="r1", scheduler="slurm")
    watcher_install(experiment_dir=tmp_path, run_id="r1", scheduler="slurm")

    # Every registration strips the prior marker line before appending, so a
    # re-install can never duplicate — the command is grep -vF <marker> | crontab -.
    for reg in ssh.dispatched("| crontab -"):
        assert "grep -vF" in reg
        assert "hpc-agent-watcher run_id=r1" in reg


# ── uninstall + status ───────────────────────────────────────────────────────


def test_uninstall_strips_markers_and_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upsert_run(tmp_path, _record())
    ssh = _ScriptedSSH([])
    _patch_ssh(monkeypatch, ssh)

    out = watcher_install(
        experiment_dir=tmp_path, run_id="r1", scheduler="slurm", action="uninstall"
    )

    assert out["installed"] is False
    assert ssh.dispatched("crontab -l") and ssh.dispatched("scrontab -l")
    rm = ssh.dispatched("rm -f")
    assert rm and ".hpc/watcher/hpc_watcher.py" in rm[0]
    assert ".hpc_last_read" in rm[0]


def test_status_reports_installed_cron(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    upsert_run(tmp_path, _record())
    ssh = _ScriptedSSH([("grep -Fq", (0, "YES\n", ""))])
    _patch_ssh(monkeypatch, ssh)

    out = watcher_install(experiment_dir=tmp_path, run_id="r1", scheduler="slurm", action="status")

    assert out["installed"] is True
    assert out["mechanism"] == "cron"


def test_missing_record_raises(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid):
        watcher_install(experiment_dir=tmp_path, run_id="nope", scheduler="slurm")


def test_result_model_shape() -> None:
    """The primitive's return dict validates against WatcherInstallResult."""
    r = WatcherInstallResult(
        run_id="r1",
        action="install",
        installed=True,
        mechanism="cron",
        reason="x",
        detail="y",
        probes={"crontab": "viable"},
    )
    assert r.model_dump(mode="json")["mechanism"] == "cron"
    # Spec validates run_id + scheduler shape.
    assert WatcherInstallSpec(run_id="r1", scheduler="slurm").stale_sec == 1800


# ── the watcher script, executed for real (stdlib-only) ──────────────────────


def _run_watcher(run_dir: Path, *, stale_sec: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(WATCHER_SCRIPT),
            "--run-dir",
            str(run_dir),
            "--stale-sec",
            str(stale_sec),
            "--job-name",
            "jobx",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_watcher_alarms_when_last_read_missing(tmp_path: Path) -> None:
    proc = _run_watcher(tmp_path, stale_sec=1800)
    assert proc.returncode == 0, proc.stderr
    status = json.loads((tmp_path / ".hpc_watcher_status.json").read_text())
    assert status["alarm"] is True
    assert status["job_name"] == "jobx"
    alarm = (tmp_path / ".hpc_watcher_ALARM").read_text()
    assert "never stamped" in alarm


def test_watcher_alarms_when_last_read_stale(tmp_path: Path) -> None:
    marker = tmp_path / ".hpc_last_read"
    marker.write_text("")
    old = time.time() - 4000
    import os

    os.utime(marker, (old, old))
    proc = _run_watcher(tmp_path, stale_sec=1800)
    assert proc.returncode == 0
    assert (tmp_path / ".hpc_watcher_ALARM").exists()
    status = json.loads((tmp_path / ".hpc_watcher_status.json").read_text())
    assert status["alarm"] is True
    assert status["last_read_age_sec"] >= 1800


def test_watcher_no_alarm_and_heals_when_fresh(tmp_path: Path) -> None:
    # Pre-existing ALARM from an earlier stale firing.
    (tmp_path / ".hpc_watcher_ALARM").write_text("stale from before\n")
    (tmp_path / ".hpc_last_read").write_text("")  # fresh (mtime ~ now)
    proc = _run_watcher(tmp_path, stale_sec=1800)
    assert proc.returncode == 0
    status = json.loads((tmp_path / ".hpc_watcher_status.json").read_text())
    assert status["alarm"] is False
    # A fresh client read heals the stale ALARM.
    assert not (tmp_path / ".hpc_watcher_ALARM").exists()
