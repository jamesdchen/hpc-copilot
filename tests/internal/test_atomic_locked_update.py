"""Tests for ``hpc_agent._internal.io.atomic_locked_update``.

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
from pathlib import Path

import pytest

from hpc_agent._internal.io import atomic_locked_update


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
    ctx = multiprocessing.get_context("fork")
    with ctx.Pool(processes=n) as pool:
        pool.map(_concurrent_appender, args)

    final = json.loads(path.read_text())
    assert isinstance(final["entries"], list)
    assert sorted(final["entries"]) == list(range(n))
