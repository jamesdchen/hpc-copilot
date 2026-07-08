"""Tests for the content-keyed evidence digest cache (T3, E-cache)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from hpc_agent.state import evidence_cache as ec


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the journal home to a scratch dir; ensure the opt-out is unset."""
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "home"))
    monkeypatch.delenv("HPC_NO_EVIDENCE_CACHE", raising=False)
    return tmp_path


def _seed_stores(experiment_dir: Path) -> None:
    """Create one file under each of the collector's five stores."""
    files = [
        experiment_dir / ".hpc" / "conclusions" / "c1.decisions.jsonl",
        experiment_dir / ".hpc" / "scopes" / "edge-x.decisions.jsonl",
        experiment_dir / ".hpc" / "scopes" / "edge-x.looks.jsonl",
        experiment_dir / ".hpc" / "campaigns" / "camp1" / "decisions.jsonl",
        experiment_dir / ".hpc" / "runs" / "run1.json",
        experiment_dir / "_aggregated" / "_fingerprints" / "abc123.jsonl",
    ]
    for f in files:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text('{"ts": "2025-01-01"}\n', encoding="utf-8")


# --- fingerprint / key ------------------------------------------------------


def test_store_globs_covers_five_stores() -> None:
    assert ec.STORE_GLOBS  # non-empty, importable by T1
    # every glob is relative and non-creating (no leading slash, no `..`)
    for g in ec.STORE_GLOBS:
        assert not g.startswith("/")
        assert ".." not in g


def test_fingerprint_fresh_namespace_is_empty_and_noncreating(tmp_path: Path) -> None:
    exp = tmp_path / "exp"
    exp.mkdir()
    assert ec.store_fingerprint(exp) == []
    # glob must not have materialized any store directory
    assert list(exp.iterdir()) == []


def test_fingerprint_uses_mtime_ns(tmp_path: Path) -> None:
    exp = tmp_path / "exp"
    _seed_stores(exp)
    fp = ec.store_fingerprint(exp)
    assert fp, "expected the seeded stores to be fingerprinted"
    for relpath, mtime_ns, size in fp:
        assert isinstance(relpath, str)
        # nanosecond precision (win32-safe): matches os.stat().st_mtime_ns
        real = os.stat(exp / relpath).st_mtime_ns
        assert mtime_ns == real
        assert size == (exp / relpath).stat().st_size


def test_fingerprint_relpaths_are_posix(tmp_path: Path) -> None:
    exp = tmp_path / "exp"
    _seed_stores(exp)
    for relpath, _, _ in ec.store_fingerprint(exp):
        assert "\\" not in relpath


def test_key_moves_on_walked_file_mtime_change(tmp_path: Path) -> None:
    exp = tmp_path / "exp"
    _seed_stores(exp)
    spec = {"tags": ["edge-x"], "fleet": False}
    k1 = ec.compute_key(spec, ec.store_fingerprint(exp))

    # touch a walked file → new mtime_ns → new key
    target = exp / ".hpc" / "runs" / "run1.json"
    time.sleep(0.01)
    new_ns = os.stat(target).st_mtime_ns + 1_000_000
    os.utime(target, ns=(new_ns, new_ns))

    k2 = ec.compute_key(spec, ec.store_fingerprint(exp))
    assert k1 != k2


def test_key_moves_on_spec_change(tmp_path: Path) -> None:
    exp = tmp_path / "exp"
    _seed_stores(exp)
    fp = ec.store_fingerprint(exp)
    k1 = ec.compute_key({"tags": ["edge-x"]}, fp)
    k2 = ec.compute_key({"tags": ["edge-y"]}, fp)
    assert k1 != k2


def test_key_moves_on_pkg_version_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exp = tmp_path / "exp"
    _seed_stores(exp)
    fp = ec.store_fingerprint(exp)
    spec = {"tags": ["edge-x"]}

    monkeypatch.setattr(ec, "_pkg_version", lambda: "1.0.0")
    k1 = ec.compute_key(spec, fp)
    monkeypatch.setattr(ec, "_pkg_version", lambda: "2.0.0")
    k2 = ec.compute_key(spec, fp)
    assert k1 != k2


def test_key_is_deterministic_under_dict_order(tmp_path: Path) -> None:
    exp = tmp_path / "exp"
    _seed_stores(exp)
    fp = ec.store_fingerprint(exp)
    k1 = ec.compute_key({"tags": ["edge-x"], "fleet": False}, fp)
    k2 = ec.compute_key({"fleet": False, "tags": ["edge-x"]}, fp)
    assert k1 == k2


# --- store / read round-trip ------------------------------------------------


def test_store_then_hit_round_trip(home: Path) -> None:
    payload = {"conclusions": [], "render": "digest", "cache": "miss"}
    key = "deadbeefcafef00d" + "0" * 48
    assert ec.cached_result(key) is None  # miss before store
    ec.store_result(key, payload)
    assert ec.cached_result(key) == payload


def test_lookup_reports_state(home: Path) -> None:
    key = "a" * 64
    assert ec.lookup(key) == ("miss", None)
    payload = {"render": "x"}
    ec.store_result(key, payload)
    assert ec.lookup(key) == ("hit", payload)


def test_deleted_cache_dir_misses_then_recomputes_byte_identically(home: Path) -> None:
    key = "b" * 64
    payload = {"render": "identical", "conclusions": [{"id": "c1"}]}
    ec.store_result(key, payload)
    path = ec._cache_path(key)
    first_bytes = path.read_bytes()
    assert ec.cached_result(key) == payload

    # delete the whole cache dir — disposable, loses nothing
    import shutil

    shutil.rmtree(path.parent)
    assert ec.cached_result(key) is None  # miss
    assert ec.lookup(key) == ("miss", None)

    # recompute (same payload) stores byte-identical bytes
    ec.store_result(key, payload)
    assert path.read_bytes() == first_bytes


# --- opt-out ----------------------------------------------------------------


def test_env_optout_disables_reads_and_writes(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key = "c" * 64
    payload = {"render": "x"}
    ec.store_result(key, payload)  # seed a real entry while enabled
    path = ec._cache_path(key)
    assert path.exists()

    monkeypatch.setenv("HPC_NO_EVIDENCE_CACHE", "1")
    assert ec.cache_disabled() is True
    # disabled → never reads (returns None / "disabled") even though a file exists
    assert ec.cached_result(key) is None
    assert ec.lookup(key) == ("disabled", None)

    # disabled → never writes (no new file for a fresh key)
    fresh = "d" * 64
    ec.store_result(fresh, {"render": "y"})
    assert not ec._cache_path(fresh).exists()


def test_corrupted_cache_file_is_silent_miss(home: Path) -> None:
    key = "e" * 64
    path = ec._cache_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    # decode error falls through silently to a miss, never raises
    assert ec.cached_result(key) is None
    assert ec.lookup(key) == ("miss", None)


def test_non_dict_cache_payload_is_miss(home: Path) -> None:
    key = "f" * 64
    path = ec._cache_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert ec.cached_result(key) is None
