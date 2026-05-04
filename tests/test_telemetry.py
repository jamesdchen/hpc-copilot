"""Tests for :mod:`claude_hpc._internal.telemetry`.

Focused on the two behaviours that matter cross-process:

* The flock-guarded ``monitor-jsonl`` writer produces no torn lines
  under concurrent appenders.
* Sinks other than ``monitor-jsonl`` don't require the path argument.
* Default sink is ``"none"`` (silent) — production runs with no env var
  must not pollute stderr.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from claude_hpc._internal import telemetry


def test_default_sink_is_silent(capsys, tmp_path: Path) -> None:
    # Unset the env var explicitly to defend against test-runner pollution.
    import os

    os.environ.pop("HPC_TELEMETRY_SINK", None)
    telemetry.record("tick", {"run_id": "x", "n": 1})
    out = capsys.readouterr()
    assert out.err == ""
    assert out.out == ""


def test_stderr_jsonl_emits_one_line(capsys) -> None:
    telemetry.record("tick", {"run_id": "x", "n": 1}, sink="stderr-jsonl")
    out = capsys.readouterr()
    line = out.err.strip()
    parsed = json.loads(line)
    assert parsed == {"event": "tick", "run_id": "x", "n": 1}


def test_monitor_jsonl_requires_path() -> None:
    with pytest.raises(ValueError):
        telemetry.record("tick", {"run_id": "x"}, sink="monitor-jsonl")


def test_monitor_jsonl_appends(tmp_path: Path) -> None:
    target = tmp_path / "x.monitor.jsonl"
    telemetry.record(
        "tick",
        {"run_id": "x", "n": 1},
        sink="monitor-jsonl",
        monitor_jsonl_path=target,
    )
    telemetry.record(
        "tick",
        {"run_id": "x", "n": 2},
        sink="monitor-jsonl",
        monitor_jsonl_path=target,
    )
    lines = target.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["n"] == 1
    assert json.loads(lines[1])["n"] == 2


def test_concurrent_appenders_produce_no_torn_lines(tmp_path: Path) -> None:
    """A9 invariant: two threads appending 200 records each should
    produce 400 well-formed JSON lines, no half-written records."""
    target = tmp_path / "x.monitor.jsonl"
    N = 200
    threads_n = 2
    payload = {"run_id": "x", "filler": "x" * 256}  # big enough to fault

    def worker(tag: str) -> None:
        for i in range(N):
            telemetry.record(
                "tick",
                {**payload, "tag": tag, "i": i},
                sink="monitor-jsonl",
                monitor_jsonl_path=target,
            )

    threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = target.read_text().splitlines()
    assert len(lines) == threads_n * N
    # Every line must parse as JSON with the expected keys.
    for line in lines:
        rec = json.loads(line)
        assert rec["event"] == "tick"
        assert rec["run_id"] == "x"
        assert isinstance(rec["i"], int)
