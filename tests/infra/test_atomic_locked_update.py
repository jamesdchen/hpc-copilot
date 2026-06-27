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

from hpc_agent.infra.io import advisory_flock, atomic_locked_update


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


@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="fcntl-based locking; Windows would race here",
)
def test_concurrent_writers_serialize(tmp_path: Path) -> None:
    """Spawn 8 processes each appending one value; all 8 must land."""
    path = tmp_path / "doc.json"
    path.write_text(json.dumps({"entries": []}))

    n = 8
    args = [(str(path), i) for i in range(n)]
    # "spawn", not "fork": this test runs inside a pytest-xdist worker, and a
    # fork-based Pool inherits that already-threaded process — the classic
    # fork-after-threads deadlock, which hung this test to the 300s timeout
    # intermittently in CI. spawn starts clean children (``_concurrent_appender``
    # is module-level, so it re-imports + pickles fine).
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(processes=n) as pool:
        pool.map(_concurrent_appender, args)

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
    reason="msvcrt byte-range locking is the win32-only branch; POSIX uses fcntl",
)
def test_advisory_flock_serializes_cross_process_win32(tmp_path: Path) -> None:
    """A second process cannot acquire the lock while the first holds it (win32).

    Proves the msvcrt branch is a REAL cross-process exclusion, not the old
    permissions-only no-op: while a child holds the blocking lock, a
    ``blocking=False`` acquire yields ``False``; once the child releases,
    the same non-blocking acquire yields ``True``.
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
