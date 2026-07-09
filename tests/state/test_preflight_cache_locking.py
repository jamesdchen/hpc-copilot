"""``record_preflight`` holds the advisory flock across its read-modify-write.

Mirrors the ``canary_cache.record_canary_validated`` locked-RMW fix (and its
tests in ``tests/ops/test_submit_flow_canary_skip.py``): the pre-fix unlocked
read → mutate → write let two concurrent submits recording DIFFERENT keys
lost-update each other (read {A} / read {A} → write {A,B} clobbers {A,C}).
"""

from __future__ import annotations

import pytest

from hpc_agent.state import preflight_cache


@pytest.fixture(autouse=True)
def _redirect_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path))
    monkeypatch.delenv("HPC_NO_PREFLIGHT_CACHE", raising=False)
    monkeypatch.delenv("HPC_PREFLIGHT_TTL_SEC", raising=False)


def _key(host: str) -> str:
    return preflight_cache.preflight_cache_key(host=host, activation="m|s|env", version="0.10.5")


def test_sequential_records_preserve_both_entries():
    """Read-modify-write is locked (no lost update): two records with DIFFERENT
    keys must BOTH persist."""
    key_a = _key("hoffman2")
    key_b = _key("discovery")
    preflight_cache.record_preflight(key_a, checks=["uv_present"])
    preflight_cache.record_preflight(key_b, checks=["uv_present"])
    # The second write read the first's entry under the lock, so neither is lost.
    assert preflight_cache.is_preflight_fresh(key_a) is True
    assert preflight_cache.is_preflight_fresh(key_b) is True


def test_record_acquires_lock_around_write(monkeypatch):
    """The record write holds the advisory flock across read+write (the state-layer
    lock idiom). Assert the lock context is entered exactly once per record."""
    calls: list[str] = []
    from hpc_agent.infra import io as _io

    orig = _io.advisory_flock

    def _spy(lock_path, **kw):
        calls.append(str(lock_path))
        return orig(lock_path, **kw)

    monkeypatch.setattr(_io, "advisory_flock", _spy)
    preflight_cache.record_preflight(_key("hoffman2"))
    assert len(calls) == 1
    assert calls[0].endswith(".lock")
