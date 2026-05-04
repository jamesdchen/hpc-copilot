"""A9: regression — concurrent appenders to ``<run_id>.monitor.jsonl``
must not produce torn lines.

The monitor-flow workflow primitive and the ``/monitor-hpc`` slash
command both append JSONL ticks to the same file. ``_append_tick``
takes an exclusive flock to serialize writes; without it, two writers
hammering the file from different threads/processes interleave bytes
mid-line and leave un-parseable JSON.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

# Import at module load (not lazily inside threads) so the test never
# trips on a not-yet-installed package state in workers.
from claude_hpc.orchestrator.monitor_flow import _flock_append


def _append_one(path: Path, n: int, run_id: str) -> None:
    """Use the production helper directly so the lock pattern is exercised."""
    payload = {"run_id": run_id, "n": n, "padding": "x" * 200}
    with _flock_append(path), path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload) + "\n")


@pytest.mark.skipif(sys.platform.startswith("win"), reason="flock unavailable on Windows")
def test_concurrent_appends_produce_no_torn_lines(tmp_path: Path) -> None:
    """Two threads each append 200 lines; every line must parse as JSON."""
    target = tmp_path / "20260101-000000-deadbee.monitor.jsonl"

    def worker(run_id: str, count: int) -> None:
        for i in range(count):
            _append_one(target, i, run_id)

    t1 = threading.Thread(target=worker, args=("alpha", 200))
    t2 = threading.Thread(target=worker, args=("beta", 200))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 400
    for ln in lines:
        json.loads(ln)


@pytest.mark.skipif(sys.platform.startswith("win"), reason="flock unavailable on Windows")
def test_lock_sibling_file_created(tmp_path: Path) -> None:
    """The ``.lock`` sibling file is created on first acquire and persists."""
    target = tmp_path / "run.monitor.jsonl"
    with _flock_append(target):
        pass
    assert (tmp_path / "run.monitor.jsonl.lock").is_file()
