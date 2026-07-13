"""Tests for :func:`hpc_agent.infra.io.atomic_write_text` and
:func:`hpc_agent.infra.io.atomic_replace_path` (generator G12).

Both siblings of ``atomic_write_json`` must give the same guarantee: a durable
artifact is either the previous good bytes or the fully-written new bytes, never
a torn file — so a crash / kill mid-write can never destroy the previously
sealed artifact.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from hpc_agent.infra import io


def test_write_text_writes_exact_bytes(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    text = '{\n  "a": 1\n}\n'
    io.atomic_write_text(path, text)
    # Exact bytes round-trip — no newline translation, no re-serialization.
    assert path.read_bytes() == text.encode("utf-8")
    # No temp sibling left behind.
    assert [p for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


def test_write_text_preserves_prior_file_on_write_failure(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "settings.json"
    io.atomic_write_text(path, "OLD")

    def boom(src, dst):
        raise RuntimeError("crash mid-swap")

    monkeypatch.setattr(io, "_replace_with_retry", boom)
    with pytest.raises(RuntimeError):
        io.atomic_write_text(path, "NEW")

    # The previously sealed bytes survive; the failed write left no temp file.
    assert path.read_text(encoding="utf-8") == "OLD"
    assert [p for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


def test_replace_path_swaps_a_zip_atomically(tmp_path: Path) -> None:
    archive = tmp_path / "run.zip"
    with (
        io.atomic_replace_path(archive) as tmp_archive,
        zipfile.ZipFile(tmp_archive, "w") as zf,
    ):
        zf.writestr("manifest.json", "{}")
    assert archive.exists()
    with zipfile.ZipFile(archive) as zf:
        assert zf.namelist() == ["manifest.json"]
    assert [p for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


def test_replace_path_preserves_prior_archive_on_failure(tmp_path: Path) -> None:
    archive = tmp_path / "run.zip"
    # Seal a first good archive.
    with (
        io.atomic_replace_path(archive) as tmp_archive,
        zipfile.ZipFile(tmp_archive, "w") as zf,
    ):
        zf.writestr("good.txt", "v1")

    # A crash mid-build must NOT destroy the sealed archive.
    with pytest.raises(RuntimeError):  # noqa: SIM117 - the raise must sit between the two contexts
        with (
            io.atomic_replace_path(archive) as tmp_archive,
            zipfile.ZipFile(tmp_archive, "w") as zf,
        ):
            zf.writestr("partial.txt", "v2")
            raise RuntimeError("crash before the with-block swap")

    with zipfile.ZipFile(archive) as zf:
        assert zf.namelist() == ["good.txt"]
        assert zf.read("good.txt") == b"v1"
    assert [p for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []
