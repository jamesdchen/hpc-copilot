"""Tests for hpc_mapreduce/templates/starters/chunking_shim.py."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

_SHIM_PATH = (
    Path(__file__).parent.parent
    / "hpc_mapreduce" / "templates" / "starters" / "chunking_shim.py"
)


def _load_shim():
    spec = importlib.util.spec_from_file_location("chunking_shim", _SHIM_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def shim():
    return _load_shim()


def test_cached_total_items_writes_to_unique_tempfile(shim, tmp_path, monkeypatch):
    """The cache writer must use a per-process tempfile, not a shared
    `_shim_cache.json.tmp`, otherwise concurrent array tasks racing on a
    shared filesystem will collide on os.replace.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(shim, "_compute_total_items", lambda: 1234)

    captured: dict[str, object] = {}
    real_mkstemp = shim.tempfile.mkstemp

    def spy_mkstemp(*args, **kwargs):
        captured["called"] = True
        captured["kwargs"] = kwargs
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(shim.tempfile, "mkstemp", spy_mkstemp)

    total = shim._cached_total_items()
    assert total == 1234
    assert captured.get("called") is True
    # The shared-name tempfile must NOT exist (our writer used mkstemp instead).
    assert not (tmp_path / "_shim_cache.json.tmp").exists()
    # The final cache file must be present and parseable.
    cache = json.loads((tmp_path / "_shim_cache.json").read_text())
    assert cache["total_items"] == 1234


def test_cached_total_items_uses_cache_on_second_call(shim, tmp_path, monkeypatch):
    """Once `_shim_cache.json` exists, _compute_total_items must not be re-run."""
    monkeypatch.chdir(tmp_path)
    calls = {"n": 0}

    def fake_compute():
        calls["n"] += 1
        return 7

    monkeypatch.setattr(shim, "_compute_total_items", fake_compute)

    assert shim._cached_total_items() == 7
    assert shim._cached_total_items() == 7
    assert calls["n"] == 1


def test_tempfile_is_cleaned_up_on_serialisation_error(shim, tmp_path, monkeypatch):
    """If json.dump raises mid-write, the tempfile must not leak."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(shim, "_compute_total_items", lambda: 42)

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated serialisation failure")

    monkeypatch.setattr(shim.json, "dump", boom)

    with pytest.raises(RuntimeError):
        shim._cached_total_items()

    # No tempfile, no final file.
    leftovers = [p for p in os.listdir(tmp_path) if p.startswith("_shim_cache.")]
    assert leftovers == [], f"tempfile leaked: {leftovers}"
