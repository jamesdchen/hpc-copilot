"""Tree-fingerprint cache for discover-runs (#264)."""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_agent.executor_cli import Flag
from hpc_agent.experiment_kit.discover import RunInfo
from hpc_agent.state import discover_cache


@pytest.fixture(autouse=True)
def _redirect_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.delenv("HPC_NO_DISCOVER_CACHE", raising=False)


def _exp(tmp_path) -> Path:
    exp = tmp_path / "exp"
    exp.mkdir()
    (exp / "run.py").write_text("# @register_run\n", encoding="utf-8")
    return exp


def _infos(exp: Path, *, flag_type=int):
    return [
        RunInfo(
            path=exp / "run.py",
            name="estimate_pi",
            gpu=False,
            flags=(Flag(name="seed", type=flag_type, default=0, help="seed"),),
            run_signature_sha="a" * 64,
        )
    ]


def test_roundtrip(tmp_path):
    exp = _exp(tmp_path)
    infos = _infos(exp)
    assert discover_cache.load(exp) is None  # miss
    discover_cache.store(exp, infos)
    loaded = discover_cache.load(exp)
    assert loaded == infos  # frozen dataclasses compare by value


def test_edit_invalidates(tmp_path):
    exp = _exp(tmp_path)
    discover_cache.store(exp, _infos(exp))
    assert discover_cache.load(exp) is not None
    # Editing a source file changes its size/mtime → fingerprint mismatch → miss.
    (exp / "run.py").write_text("# @register_run\nx = 1\n", encoding="utf-8")
    assert discover_cache.load(exp) is None


def test_new_file_invalidates(tmp_path):
    exp = _exp(tmp_path)
    discover_cache.store(exp, _infos(exp))
    (exp / "another.py").write_text("y = 2\n", encoding="utf-8")
    assert discover_cache.load(exp) is None


def test_disable_env(tmp_path, monkeypatch):
    exp = _exp(tmp_path)
    discover_cache.store(exp, _infos(exp))
    assert discover_cache.load(exp) is not None
    monkeypatch.setenv("HPC_NO_DISCOVER_CACHE", "1")
    assert discover_cache.load(exp) is None


def test_exotic_flag_type_is_not_cached(tmp_path):
    # A Flag.type we can't round-trip (list) → store is a no-op (safe fallback).
    exp = _exp(tmp_path)
    discover_cache.store(exp, _infos(exp, flag_type=list))
    assert discover_cache.load(exp) is None


def test_skip_dirs_excluded_from_fingerprint(tmp_path):
    # A change inside a skipped dir (.venv) must NOT invalidate (discover skips it too).
    exp = _exp(tmp_path)
    discover_cache.store(exp, _infos(exp))
    venv = exp / ".venv"
    venv.mkdir()
    (venv / "junk.py").write_text("import this\n", encoding="utf-8")
    assert discover_cache.load(exp) is not None


def test_discover_runs_uses_cache(tmp_path, monkeypatch):
    from hpc_agent.state import discover as state_discover

    exp = _exp(tmp_path)
    calls: list[Path] = []

    def _spy(src_dir):
        calls.append(Path(src_dir))
        return []

    monkeypatch.setattr("hpc_agent.experiment_kit.discover.discover_runs", _spy)

    first = state_discover.discover_runs(exp)
    second = state_discover.discover_runs(exp)
    assert first == second == []
    # The scan ran once; the second call was served from cache.
    assert calls == [exp]
