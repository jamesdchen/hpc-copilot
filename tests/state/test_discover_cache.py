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
            mpi=False,
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


def test_sequential_stores_preserve_both_experiments(tmp_path):
    """Read-modify-write is locked (no lost update): two stores keyed by
    DIFFERENT experiment dirs in the shared global file must BOTH persist
    (the same unlocked-RMW lost-update its siblings ``canary_cache`` /
    ``preflight_cache`` were fixed for)."""
    exp_a = _exp(tmp_path)
    exp_b = tmp_path / "exp_b"
    exp_b.mkdir()
    (exp_b / "run.py").write_text("# @register_run\n", encoding="utf-8")
    discover_cache.store(exp_a, _infos(exp_a))
    discover_cache.store(exp_b, _infos(exp_b))
    # The second store read the first's entry under the lock, so neither is lost.
    assert discover_cache.load(exp_a) is not None
    assert discover_cache.load(exp_b) is not None


def test_store_acquires_lock_around_write(tmp_path, monkeypatch):
    """``store`` holds the advisory flock across read+write (the state-layer
    lock idiom). Assert the lock context is entered exactly once per store."""
    calls: list[str] = []
    from hpc_agent.infra import io as _io

    orig = _io.advisory_flock

    def _spy(lock_path, **kw):
        calls.append(str(lock_path))
        return orig(lock_path, **kw)

    monkeypatch.setattr(_io, "advisory_flock", _spy)
    exp = _exp(tmp_path)
    discover_cache.store(exp, _infos(exp))
    assert len(calls) == 1
    assert calls[0].endswith(".lock")


def test_fingerprint_skip_dirs_subset_of_scan_skip_dirs() -> None:
    """The fingerprint must not prune a directory the actual source scan reads.

    If ``discover_cache._SKIP_DIRS`` skips a dir that ``state.discover`` /
    ``experiment_kit.discover`` still walk, a ``@register_run`` edit under it
    (e.g. a run vendored inside ``.venv`` / ``.claude``) would change live
    results without changing the fingerprint → a stale cache is served.
    """
    from hpc_agent.experiment_kit import discover as ek_discover
    from hpc_agent.state import discover as state_discover

    scan_skips = state_discover._SKIP_DIRS | ek_discover._SKIP_DIRS
    extra = discover_cache._SKIP_DIRS - scan_skips
    assert not extra, (
        "discover_cache._SKIP_DIRS prunes dirs the scan still reads: "
        f"{sorted(extra)}. A register_run edit under these would not change the "
        "fingerprint, serving a stale cache."
    )
