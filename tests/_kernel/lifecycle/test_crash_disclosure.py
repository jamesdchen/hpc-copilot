"""Worker-side crash disclosure (run-#13 finding 2).

A detached worker died exit-2 (a VPN-severed scp child) and flushed NOTHING to its
log — no traceback, no exit code — so the terminal's "the log carries the disclosed
failure" was false. These pin the WORKER-SIDE seam: every exit path leaves a
``[fatal]`` block, and the honest-terminal read side reports what the log actually
contains.

In-process and hermetic (injectable streams / real files), matching the existing
``tests/_kernel/lifecycle`` patterns — plus one real-subprocess test that the hard
signal path (``os.abort``) dumps a native traceback to the log via faulthandler.
"""

from __future__ import annotations

import io
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from hpc_agent._kernel.lifecycle import crash_disclosure as cd


@pytest.fixture
def _worker_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_DETACHED_RUN_ID", "run-x")


# ─── emit_fatal_block ──────────────────────────────────────────────────────


def test_emit_fatal_block_writes_exception_and_traceback(_worker_env: None) -> None:
    stream = io.StringIO()
    try:
        raise RuntimeError("scp connection lost")
    except RuntimeError as exc:
        wrote = cd.emit_fatal_block(
            exc=exc,
            last_stage="[hb] alive 480s | child=ssh.exe cpu=17.2s",
            stream=stream,
        )
    out = stream.getvalue()
    assert wrote is True
    assert cd.FATAL_MARKER in out
    assert "RuntimeError: scp connection lost" in out
    assert "Traceback (most recent call last)" in out  # bounded traceback tail
    assert "last known stage: [hb] alive 480s | child=ssh.exe" in out


def test_emit_fatal_block_writes_exit_code(_worker_env: None) -> None:
    stream = io.StringIO()
    wrote = cd.emit_fatal_block(exit_code=2, last_stage=None, stream=stream)
    out = stream.getvalue()
    assert wrote is True
    assert cd.FATAL_MARKER in out
    assert "exit_code=2" in out


def test_emit_fatal_block_bounds_a_huge_traceback(
    _worker_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A pathological traceback can be megabytes; the block keeps only the tail
    # (the actual failure frame). Stub the formatter to a long string so the
    # bounding branch is exercised directly (real recursion tracebacks collapse).
    monkeypatch.setattr(cd.traceback, "format_exception", lambda *a, **k: "x" * 20_000)
    stream = io.StringIO()
    cd.emit_fatal_block(exc=ValueError("bottom"), stream=stream)
    out = stream.getvalue()
    assert "ValueError: bottom" in out
    assert "traceback head elided" in out
    assert len(out) < cd._TRACEBACK_TAIL_CHARS + 2000


def test_emit_fatal_block_noop_outside_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HPC_DETACHED_RUN_ID", raising=False)
    stream = io.StringIO()
    wrote = cd.emit_fatal_block(exc=RuntimeError("x"), exit_code=3, stream=stream)
    assert wrote is False
    assert stream.getvalue() == ""


def test_emit_fatal_block_swallows_a_raising_stream(_worker_env: None) -> None:
    class _Raising(io.StringIO):
        def write(self, _s: str) -> int:  # type: ignore[override]
            raise OSError("disk full")

    # The exit path must never gain a new crash.
    assert cd.emit_fatal_block(exit_code=2, stream=_Raising()) is False


def test_install_crash_faulthandler_noop_outside_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HPC_DETACHED_RUN_ID", raising=False)
    # Must not arm faulthandler on the foreground process; a no-op that never raises.
    cd.install_crash_faulthandler(stream=io.StringIO())


# ─── log_has_fatal_marker (the honest-terminal read side) ──────────────────


def test_log_has_fatal_marker_true(tmp_path: Path) -> None:
    log = tmp_path / "w.log"
    log.write_text(
        "[hb] alive 30s | child=ssh.exe cpu=1.0s\n"
        "[fatal] detached worker exit-path disclosure\n"
        "[fatal] exit_code=2\n",
        encoding="utf-8",
    )
    has_fatal, last_line = cd.log_has_fatal_marker(log)
    assert has_fatal is True
    assert last_line == "[fatal] exit_code=2"


def test_log_has_fatal_marker_false_reports_last_line(tmp_path: Path) -> None:
    log = tmp_path / "w.log"
    log.write_text(
        "[hb] alive 480s | child=ssh.exe cpu=17.2s\n"
        "[transport] progress: 355 MB / ~1181 MB (30%), elapsed 300s\n",
        encoding="utf-8",
    )
    has_fatal, last_line = cd.log_has_fatal_marker(log)
    assert has_fatal is False
    assert last_line == "[transport] progress: 355 MB / ~1181 MB (30%), elapsed 300s"


def test_log_has_fatal_marker_missing_log(tmp_path: Path) -> None:
    assert cd.log_has_fatal_marker(tmp_path / "absent.log") == (False, "")
    assert cd.log_has_fatal_marker(None) == (False, "")


def test_log_has_fatal_marker_reads_bounded_tail(tmp_path: Path) -> None:
    """A [fatal] block flushed at the very end is found even after megabytes of
    heartbeat lines; a marker buried far above the tail bound is not (the bounded
    read is intentional)."""
    log = tmp_path / "w.log"
    filler = "[hb] alive 1s | no children\n" * 5000
    log.write_text(filler + "[fatal] exit_code=2\n", encoding="utf-8")
    has_fatal, last_line = cd.log_has_fatal_marker(log)
    assert has_fatal is True
    assert last_line == "[fatal] exit_code=2"


# ─── the hard-signal path: faulthandler dumps to the log on os.abort ───────


def test_faulthandler_dumps_native_traceback_on_hard_signal(tmp_path: Path) -> None:
    """A killed/crashing worker (no Python ``except`` can catch a SIGABRT) still
    leaves a native traceback in its log — the hard-death disclosure the finding
    demanded. Real subprocess: arm the handler on a log file, then os.abort()."""
    log = tmp_path / "worker.log"
    script = textwrap.dedent(
        """
        import os, sys
        os.environ["HPC_DETACHED_RUN_ID"] = "run-x"
        from hpc_agent._kernel.lifecycle import crash_disclosure as cd
        with open(sys.argv[1], "w", encoding="utf-8") as log:
            cd.install_crash_faulthandler(stream=log)
            log.write("[transport] progress: mid-transfer\\n")
            log.flush()
            os.abort()  # SIGABRT — no except can catch it
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script, str(log)],
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode != 0  # it aborted
    text = log.read_text(encoding="utf-8", errors="replace")
    # faulthandler wrote a native traceback (thread dump) into the same log.
    assert "Current thread" in text or "Fatal Python error" in text or "os.abort" in text
