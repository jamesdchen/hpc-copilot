"""Tests for ``hpc_agent.infra.io.atomic_locked_update``.

The helper is the canonical read-modify-write primitive for JSON docs
under flock. These tests cover:

- creating a new doc when the file does not exist
- updating an existing doc
- concurrent writers serializing (no lost updates)
- crash mid-write (mutate raises) does not corrupt the file
- corrupt / non-JSON / non-dict files surface as ``None`` to mutate
"""

from __future__ import annotations

import json
import multiprocessing
import sys
import time
from pathlib import Path

import pytest

from hpc_agent.infra.io import advisory_flock, append_jsonl_line, atomic_locked_update


def test_create_new_doc(tmp_path: Path) -> None:
    path = tmp_path / "doc.json"
    assert not path.exists()

    seen: dict[str, object] = {}

    def mutate(doc):
        seen["initial"] = doc
        return {"schema_version": 1, "entries": ["a"]}

    out = atomic_locked_update(path, mutate)
    assert seen["initial"] is None
    assert out == {"schema_version": 1, "entries": ["a"]}
    assert json.loads(path.read_text()) == out


def test_update_existing_doc(tmp_path: Path) -> None:
    path = tmp_path / "doc.json"
    path.write_text(json.dumps({"entries": [1]}))

    def mutate(doc):
        assert doc == {"entries": [1]}
        doc["entries"].append(2)
        return doc

    out = atomic_locked_update(path, mutate)
    assert out == {"entries": [1, 2]}
    assert json.loads(path.read_text()) == {"entries": [1, 2]}


def test_corrupt_file_yields_none(tmp_path: Path) -> None:
    path = tmp_path / "doc.json"
    path.write_text("not-json")

    seen: dict[str, object] = {}

    def mutate(doc):
        seen["initial"] = doc
        return {"ok": True}

    atomic_locked_update(path, mutate)
    assert seen["initial"] is None
    assert json.loads(path.read_text()) == {"ok": True}


def test_non_dict_json_yields_none(tmp_path: Path) -> None:
    path = tmp_path / "doc.json"
    path.write_text(json.dumps([1, 2, 3]))

    seen: dict[str, object] = {}

    def mutate(doc):
        seen["initial"] = doc
        return {"ok": True}

    atomic_locked_update(path, mutate)
    assert seen["initial"] is None


def test_mutate_raises_does_not_corrupt(tmp_path: Path) -> None:
    """If mutate raises, the file must keep its previous contents
    (or stay absent if it didn't exist).
    """
    path = tmp_path / "doc.json"
    path.write_text(json.dumps({"a": 1}))

    def mutate(_doc):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        atomic_locked_update(path, mutate)

    # File still has its original contents.
    assert json.loads(path.read_text()) == {"a": 1}
    # No leftover .tmp temp files in the directory either.
    leftovers = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_mutate_raises_on_missing_keeps_missing(tmp_path: Path) -> None:
    path = tmp_path / "doc.json"
    assert not path.exists()

    def mutate(_doc):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        atomic_locked_update(path, mutate)

    assert not path.exists()


def _concurrent_appender(args: tuple[str, int]) -> None:
    """Append a single integer to ``entries`` in the doc at *path*."""
    path_str, value = args

    def mutate(doc):
        doc = doc if isinstance(doc, dict) else {}
        entries = doc.get("entries")
        if not isinstance(entries, list):
            entries = []
        entries.append(value)
        doc["entries"] = entries
        return doc

    atomic_locked_update(Path(path_str), mutate)


def test_concurrent_writers_serialize(tmp_path: Path) -> None:
    """Spawn 8 processes each appending one value; all 8 must land.

    Runs on win32 too: ``atomic_locked_update`` routes through
    ``advisory_flock``, backed by the ``filelock`` library (msvcrt
    byte-range locking on win32, ``fcntl`` on POSIX under the hood), so
    concurrent writers serialize identically on every platform. This is
    a BEHAVIOR pin (no lost updates), not a mechanism pin — it must keep
    passing unchanged across any locking-backend swap.
    """
    path = tmp_path / "doc.json"
    path.write_text(json.dumps({"entries": []}))

    n = 8
    # "spawn", not "fork": this test runs inside a pytest-xdist worker, and a
    # fork-based child inherits that already-threaded process — the classic
    # fork-after-threads deadlock, which hung this test to the 300s timeout
    # intermittently in CI. spawn starts clean children (``_concurrent_appender``
    # is module-level, so it re-imports + pickles fine).
    #
    # Explicit ``Process`` children (not ``Pool.map``) with a per-child
    # ``join(timeout=60)`` + exit-code assert: a spawn child that dies during
    # bootstrap makes ``Pool.map`` block forever (the suspected un-diagnosable
    # 300s CI hang on py3.12). Here a wedged child is a named, bounded failure.
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_concurrent_appender, args=((str(path), i),)) for i in range(n)]
    for proc in procs:
        proc.start()
    try:
        for proc in procs:
            proc.join(timeout=60)
            assert not proc.is_alive(), f"child {proc.pid} did not finish within 60s"
            assert proc.exitcode == 0, f"child {proc.pid} exited {proc.exitcode}"
    finally:
        for proc in procs:
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=5)

    final = json.loads(path.read_text())
    assert isinstance(final["entries"], list)
    assert sorted(final["entries"]) == list(range(n))


def _child_hold_lock(args: tuple[str, str, str]) -> None:
    """Acquire ``advisory_flock`` (blocking), signal held, hold until released.

    Module-level so the ``spawn`` context can pickle + re-import it. The hold
    is bounded (~30s ceiling) so a missed release never wedges CI.
    """
    lock_str, held_path, release_path = args
    release = Path(release_path)
    with advisory_flock(Path(lock_str)):
        Path(held_path).write_text("held", encoding="utf-8")
        for _ in range(600):
            if release.exists():
                return
            time.sleep(0.05)


@pytest.mark.skipif(
    not sys.platform.startswith("win"),
    reason="pins the win32 lock path (filelock->msvcrt); POSIX uses filelock->fcntl",
)
def test_advisory_flock_serializes_cross_process_win32(tmp_path: Path) -> None:
    """A second process cannot acquire the lock while the first holds it (win32).

    Proves the win32 path is a REAL cross-process exclusion, not the old
    permissions-only no-op: while a child holds the blocking lock, a
    ``blocking=False`` acquire yields ``False``; once the child releases,
    the same non-blocking acquire yields ``True``. A behavior pin, agnostic
    to the backend (hand-rolled msvcrt historically; ``filelock`` today).
    """
    lock = tmp_path / "submit.lock"
    held = tmp_path / "held.flag"
    release = tmp_path / "release.flag"

    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=_child_hold_lock, args=((str(lock), str(held), str(release)),))
    proc.start()
    try:
        # Wait (≤10s) for the child to actually hold the lock.
        for _ in range(200):
            if held.exists():
                break
            time.sleep(0.05)
        assert held.exists(), "child never acquired the lock"

        # Contended: a non-blocking acquire must fail while the child holds it.
        with advisory_flock(lock, blocking=False) as got:
            assert got is False

        # Release the child; once it lets go, the lock is acquirable again.
        release.write_text("go", encoding="utf-8")
        proc.join(timeout=15)
        assert not proc.is_alive()
        with advisory_flock(lock, blocking=False) as got:
            assert got is True
    finally:
        # Belt-and-suspenders: never leave the child wedged.
        release.write_text("go", encoding="utf-8")
        if proc.is_alive():
            proc.terminate()
        proc.join(timeout=5)


def test_append_jsonl_line_concurrent_appends_stay_whole_lines(tmp_path: Path) -> None:
    """B2: the canonical JSONL-append seam serializes concurrent appenders under
    the flock, so N threads produce N WHOLE, parseable lines — never interleaved
    bytes on one line. This is the whole-line-atomicity the torn-line reader fix
    relies on for every prior line staying intact."""
    import threading

    path = tmp_path / "ledger.jsonl"
    n_threads, per_thread = 8, 25
    barrier = threading.Barrier(n_threads)

    def _worker(tid: int) -> None:
        barrier.wait()  # maximize contention
        for i in range(per_thread):
            append_jsonl_line(path, {"tid": tid, "i": i, "pad": "x" * 200})

    threads = [threading.Thread(target=_worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == n_threads * per_thread
    # Every line is a WHOLE, parseable object (no interleaving) and the full set
    # of (tid, i) pairs is present exactly once.
    pairs = {(json.loads(ln)["tid"], json.loads(ln)["i"]) for ln in lines}
    assert pairs == {(t, i) for t in range(n_threads) for i in range(per_thread)}


def test_advisory_flock_bounded_wait_raises_loud_timeout(tmp_path) -> None:
    """Run-#12 finding 16: a wedged holder froze a worker at 0 CPU for 15
    minutes with zero disclosure. A ``timeout_sec``-bounded blocking acquire
    raises a LOUD, path-naming TimeoutError instead of waiting forever."""
    import pytest as _pytest

    from hpc_agent.infra.io import advisory_flock

    lock_path = tmp_path / "held.lock"
    lock = advisory_flock(lock_path)
    # filelock reentrancy is per-instance; a second advisory_flock on the
    # same path contends at the OS lock exactly like another process.
    with (
        lock,
        _pytest.raises(TimeoutError, match="held.lock"),
        advisory_flock(lock_path, timeout_sec=0.2),
    ):
        raise AssertionError("must not acquire")  # pragma: no cover


def test_advisory_flock_no_timeout_still_blocks_by_default(tmp_path) -> None:
    """The default (timeout_sec=None) keeps the historical contract: no raise,
    acquisition succeeds once the holder releases."""
    from hpc_agent.infra.io import advisory_flock

    lock_path = tmp_path / "free.lock"
    with advisory_flock(lock_path, timeout_sec=5.0) as got:
        assert got is True
