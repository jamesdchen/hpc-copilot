"""Progress disclosure for the opaque cluster-side combine + final-reduce stages.

Run-13 finding 12(c): the detached aggregate worker ran 25+ minutes emitting
ZERO non-heartbeat lines — the per-wave combine and the final cross-wave reduce
are each a single blocking ``ssh_run`` on the login node with no incremental
output, so an active stage was indistinguishable from a hang from the tail-able
worker log. The fix wraps each stage with a wall-clock heartbeat
(:func:`run_with_stage_heartbeat`): a start line, a periodic "still running"
line for as long as the stage blocks, and an outcome-bearing end line.
"""

from __future__ import annotations

import subprocess
import time

import pytest

from hpc_agent.infra.transport import _combiner
from hpc_agent.infra.transport._disclose import run_with_stage_heartbeat


def _completed(returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args="ssh", returncode=returncode, stdout="", stderr="")


# --- the helper: fires on slow work, quiet on fast work -----------------------


def test_stage_heartbeat_fires_on_slow_work(capsys: pytest.CaptureFixture[str]) -> None:
    """A stage that blocks longer than the interval emits at least one heartbeat
    line between its start and end lines."""

    def _slow() -> str:
        time.sleep(0.06)
        return "result"

    out = run_with_stage_heartbeat("combine: wave 3", "login01", _slow, interval_sec=0.01)

    err = capsys.readouterr().err
    assert out == "result"  # result forwarded verbatim
    assert "[transport] combine: wave 3: running on login01" in err
    assert "[transport] combine: wave 3: still running on login01" in err
    assert "s elapsed" in err
    assert "[transport] combine: wave 3: done on login01" in err


def test_stage_heartbeat_fast_emits_start_end_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An instant stage never crosses the interval, so it emits only the start
    and end lines — no 'still running' heartbeat."""
    out = run_with_stage_heartbeat("final reduce", "login01", lambda: 42, interval_sec=20.0)

    err = capsys.readouterr().err
    assert out == 42
    assert "[transport] final reduce: running on login01" in err
    assert "[transport] final reduce: done on login01" in err
    assert "still running" not in err


def test_stage_heartbeat_failed_end_line_and_propagates(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A stage that raises still gets a FAILED end line, and the exception
    (e.g. a stage-timeout ``TimeoutError``) propagates unchanged."""

    def _boom() -> None:
        raise TimeoutError("stage timed out")

    with pytest.raises(TimeoutError):
        run_with_stage_heartbeat("combine: wave 0", "login01", _boom, interval_sec=20.0)

    err = capsys.readouterr().err
    assert "[transport] combine: wave 0: FAILED on login01" in err


# --- the wiring: combine + final reduce route through the heartbeat -----------


def test_run_combiner_emits_stage_lines(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``run_combiner`` wraps its ssh_run in the wave-labelled heartbeat: a fast
    (stubbed) call emits the start + done lines naming the wave and host."""
    monkeypatch.setattr(_combiner, "ssh_run", lambda cmd, **kw: _completed())

    result = _combiner.run_combiner(
        ssh_target="u@login01", remote_path="/proj", wave=2, run_id="run-abc"
    )

    err = capsys.readouterr().err
    assert result.returncode == 0
    assert "[transport] combine: wave 2: running on u@login01" in err
    assert "[transport] combine: wave 2: done on u@login01" in err


def test_run_final_reduce_emits_stage_lines(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``run_final_reduce`` wraps its ssh_run in the 'final reduce' heartbeat."""
    monkeypatch.setattr(_combiner, "ssh_run", lambda cmd, **kw: _completed())

    result = _combiner.run_final_reduce(
        ssh_target="u@login01", remote_path="/proj", run_id="run-abc"
    )

    err = capsys.readouterr().err
    assert result.returncode == 0
    assert "[transport] final reduce: running on u@login01" in err
    assert "[transport] final reduce: done on u@login01" in err


def test_run_combiner_checked_still_returns_tuple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The heartbeat wrap is transparent to ``run_combiner_checked``'s
    ``(ok, stdout, stderr)`` collapse."""
    monkeypatch.setattr(
        _combiner,
        "ssh_run",
        lambda cmd, **kw: subprocess.CompletedProcess("ssh", 0, "OUT", "ERR"),
    )

    ok, out, err = _combiner.run_combiner_checked(
        ssh_target="u@login01", remote_path="/proj", wave=1, run_id="run-abc"
    )

    assert ok is True
    assert out == "OUT"
    assert err == "ERR"
