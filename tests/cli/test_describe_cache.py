"""Version-keyed disk cache for ``hpc-agent describe`` output (#261)."""

from __future__ import annotations

import pytest

from hpc_agent.cli import setup
from hpc_agent.state import describe_cache


@pytest.fixture(autouse=True)
def _redirect_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path))
    monkeypatch.delenv("HPC_NO_DESCRIBE_CACHE", raising=False)


def test_store_then_load_roundtrip():
    payload = {"kind": "primitive", "name": "submit-flow", "content": {"verb": "workflow"}}
    assert describe_cache.load("submit-flow") is None  # miss
    describe_cache.store("submit-flow", payload)
    assert describe_cache.load("submit-flow") == payload


def test_disabled_env_is_a_miss_and_store_noop(monkeypatch):
    describe_cache.store("submit-flow", {"kind": "primitive", "name": "submit-flow"})
    monkeypatch.setenv("HPC_NO_DESCRIBE_CACHE", "1")
    assert describe_cache.load("submit-flow") is None
    describe_cache.store("other", {"kind": "skill", "name": "other"})
    monkeypatch.delenv("HPC_NO_DESCRIBE_CACHE")
    assert describe_cache.load("other") is None  # store was a no-op while disabled


def test_version_bump_invalidates(monkeypatch):
    monkeypatch.setattr(describe_cache, "_pkg_version", lambda: "1.0.0")
    describe_cache.store("submit-flow", {"kind": "primitive", "name": "submit-flow"})
    assert describe_cache.load("submit-flow") is not None
    # A new installed version lands in a fresh dir → miss.
    monkeypatch.setattr(describe_cache, "_pkg_version", lambda: "1.0.1")
    assert describe_cache.load("submit-flow") is None


def test_unsafe_name_never_caches(monkeypatch):
    # Path-traversal-shaped names must not produce a cache path.
    assert describe_cache._cache_path("../etc/passwd") is None
    assert describe_cache.load("../etc/passwd") is None


def test_emit_describe_second_call_hits_cache(monkeypatch, capsys):
    calls: list[str] = []

    def _spy_describe(*, name, _catalog=None):
        # B4/B5: _emit_describe threads a hydrated catalog into describe() so the
        # fast path never reads a partial registry. The cache still short-circuits
        # the SECOND call before describe() runs.
        calls.append(name)
        return {"kind": "primitive", "name": name, "content": {"verb": "query"}}

    monkeypatch.setattr(setup, "describe", _spy_describe)

    rc1 = setup._emit_describe("submit-flow")
    out1 = capsys.readouterr().out
    rc2 = setup._emit_describe("submit-flow")
    out2 = capsys.readouterr().out

    assert rc1 == rc2 == setup.EXIT_OK
    # describe() ran once (first call); the second served from cache.
    assert calls == ["submit-flow"]
    # And the emitted envelope is byte-identical across the two calls.
    assert out1 == out2 and out1.strip()


def test_store_refused_under_partial_registration(monkeypatch):
    # A1 poisoning guard: the single-verb fast path leaves the registry PARTIAL
    # (register_single_module sets the weaker _DISPATCH_READY latch, not
    # _REGISTRATION_DONE). A describe resolved off a partial registry is
    # wrong-but-plausible; store() must refuse to persist it, or every full-path
    # reader is poisoned for the version's lifetime.
    from hpc_agent._kernel.registry import primitive

    monkeypatch.setattr(primitive, "_REGISTRATION_DONE", False)
    describe_cache.store("submit-flow", {"kind": "primitive", "name": "submit-flow"})
    # Nothing was written — restoring full registration still yields a miss.
    monkeypatch.setattr(primitive, "_REGISTRATION_DONE", True)
    assert describe_cache.load("submit-flow") is None
    # And once full registration is in effect, store() persists as normal.
    describe_cache.store("submit-flow", {"kind": "primitive", "name": "submit-flow"})
    assert describe_cache.load("submit-flow") is not None


def test_emit_describe_not_found_is_not_cached(monkeypatch):
    def _raise(*, name, _catalog=None):
        raise ValueError(f"no skill or primitive named {name!r}")

    monkeypatch.setattr(setup, "describe", _raise)
    rc = setup._emit_describe("nope-not-real")
    assert rc != setup.EXIT_OK
    # Nothing cached for a not-found name.
    assert describe_cache.load("nope-not-real") is None
