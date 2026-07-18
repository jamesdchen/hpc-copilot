"""Tests for the ``SessionStart`` alert-count hook (proving run #3).

The scheduled ``doctor`` watchdog wrote the stalled-driver alert to
``doctor.alerts.log`` and nothing delivered it — this hook prints the
unacknowledged count into session context at startup. Notify only, fail-open,
never acknowledges, and NEVER scaffolds a journal namespace for an unrelated
cwd (finding g).
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from hpc_agent._kernel.hooks import alert_count

_TS = "2026-07-04T23:25:05+00:00"
_MSG = "hpc-agent doctor: driver stalled since 23:01, run pi-estimation-canary — re-arm?"


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    return tmp_path


def _write_alert(exp: Path, ts: str = _TS, message: str = _MSG) -> None:
    """Append one alert in the NEW canonical JSON-record format (dedup writer)."""
    from hpc_agent.state.run_record import journal_dir

    log = journal_dir(exp) / "doctor.alerts.log"
    with log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": ts, "kind": "stall", "message": message}) + "\n")


def _write_legacy_alert(exp: Path, ts: str = _TS, message: str = _MSG) -> None:
    """Append one alert in the LEGACY plaintext ``<ts> <message>`` format.

    Tolerant-reader regression: the hook must still read a pre-flip plaintext log.
    """
    from hpc_agent.state.run_record import journal_dir

    log = journal_dir(exp) / "doctor.alerts.log"
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"{ts} {message}\n")


def test_context_line_carries_count_and_newest_alert(tmp_path: Path) -> None:
    _write_alert(tmp_path)
    _write_alert(tmp_path, ts="2026-07-05T01:00:00+00:00", message="second stall")

    line = alert_count.build_context_line({"cwd": str(tmp_path)})
    assert line is not None
    assert line.startswith("2 unacknowledged hpc-agent watchdog alert(s)")
    assert "second stall" in line  # newest alert leads the pointer
    assert "hpc-agent doctor" in line  # names the surface to run


def test_context_line_reads_legacy_plaintext_alert(tmp_path: Path) -> None:
    """Tolerant-reader regression: a pre-flip legacy plaintext alert still counts."""
    _write_legacy_alert(tmp_path)
    line = alert_count.build_context_line({"cwd": str(tmp_path)})
    assert line is not None
    assert line.startswith("1 unacknowledged hpc-agent watchdog alert(s)")
    assert _MSG in line


def test_silent_when_no_alerts(tmp_path: Path) -> None:
    assert alert_count.build_context_line({"cwd": str(tmp_path)}) is None


def test_read_never_scaffolds_a_journal_namespace(tmp_path: Path) -> None:
    """Finding g: a SessionStart in an arbitrary repo must not create
    ~/.claude/hpc/<repo_hash>/ as a side effect of the read."""
    home = tmp_path / "journal"
    assert alert_count.build_context_line({"cwd": str(tmp_path / "somerepo")}) is None
    assert not home.exists() or not any(home.iterdir())


def test_main_prints_line_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_alert(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"cwd": str(tmp_path)})))
    assert alert_count.main() == 0
    out = capsys.readouterr().out
    assert "1 unacknowledged hpc-agent watchdog alert(s)" in out


def test_main_is_a_clean_noop_on_garbage_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))
    assert alert_count.main() == 0
    assert capsys.readouterr().out == ""
