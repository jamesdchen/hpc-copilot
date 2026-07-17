"""Build-content-keyed disk cache for ``hpc-agent describe`` output (#261)."""

from __future__ import annotations

import ast
import json
import subprocess
import sys

import pytest

from hpc_agent import _build_info
from hpc_agent.cli import setup
from hpc_agent.state import describe_cache


@pytest.fixture(autouse=True)
def _redirect_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path))
    monkeypatch.delenv("HPC_NO_DESCRIBE_CACHE", raising=False)
    # This repo is a source checkout (BUILD_SHA is None), where the cache is
    # DISABLED by construction. Pin a clean fake build so the roundtrip/gate
    # tests exercise the ENABLED path; the trap tests below re-patch as needed.
    monkeypatch.setattr(_build_info, "BUILD_SHA", "cafef00d")
    monkeypatch.setattr(_build_info, "BUILD_DIRTY", False)


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


def test_new_build_sha_at_same_version_invalidates(monkeypatch):
    # The fire-path regression (docket "describe-cache content-keying"): this
    # project force-reinstalls dev wheels at the SAME package version, so a
    # version-string key served STALE describe payloads. Keying the cache dir on
    # BUILD_SHA closes the trap — a new wheel gets a new sha even at an identical
    # version, so its describe lands in a fresh dir and the old payload is
    # unreachable.
    monkeypatch.setattr(_build_info, "BUILD_SHA", "aaaaaaaa")
    describe_cache.store("submit-flow", {"kind": "primitive", "name": "submit-flow"})
    assert describe_cache.load("submit-flow") is not None
    # New build, SAME version number → different sha → fresh dir → miss.
    monkeypatch.setattr(_build_info, "BUILD_SHA", "bbbbbbbb")
    assert describe_cache.load("submit-flow") is None
    # And the two shas resolve to genuinely different cache paths.
    monkeypatch.setattr(_build_info, "BUILD_SHA", "aaaaaaaa")
    path_a = describe_cache._cache_path("submit-flow")
    monkeypatch.setattr(_build_info, "BUILD_SHA", "bbbbbbbb")
    path_b = describe_cache._cache_path("submit-flow")
    assert path_a is not None and path_b is not None and path_a != path_b


def test_source_checkout_disables_cache(monkeypatch):
    # BUILD_SHA is None on a source checkout / editable install: the content is
    # not cheaply identifiable, so the cache is disabled — load misses and store
    # no-ops even for a valid payload.
    monkeypatch.setattr(_build_info, "BUILD_SHA", None)
    assert describe_cache._cache_path("submit-flow") is None
    describe_cache.store("submit-flow", {"kind": "primitive", "name": "submit-flow"})
    # Re-enable a clean build: nothing was written under the disabled build.
    monkeypatch.setattr(_build_info, "BUILD_SHA", "cafef00d")
    assert describe_cache.load("submit-flow") is None
    # And a valid payload does NOT load while disabled, even if one exists.
    describe_cache.store("submit-flow", {"kind": "primitive", "name": "submit-flow"})
    monkeypatch.setattr(_build_info, "BUILD_SHA", None)
    assert describe_cache.load("submit-flow") is None


def test_dirty_wheel_disables_cache(monkeypatch):
    # A wheel built from a dirty tree: the sha names a commit the content
    # diverged from, so g<sha> is not content-true (two dirty trees at the same
    # HEAD would collide). The cache is disabled rather than key on it.
    monkeypatch.setattr(_build_info, "BUILD_SHA", "deadbeef")
    monkeypatch.setattr(_build_info, "BUILD_DIRTY", True)
    assert describe_cache._cache_path("submit-flow") is None
    describe_cache.store("submit-flow", {"kind": "primitive", "name": "submit-flow"})
    monkeypatch.setattr(_build_info, "BUILD_DIRTY", False)
    assert describe_cache.load("submit-flow") is None  # store was a no-op while dirty


def test_unsafe_name_never_caches():
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
    # reader is poisoned for the build's lifetime.
    from hpc_agent._kernel.registry import primitive

    monkeypatch.setattr(primitive, "_REGISTRATION_DONE", False)
    describe_cache.store("submit-flow", {"kind": "primitive", "name": "submit-flow"})
    # Nothing was written — restoring full registration still yields a miss.
    monkeypatch.setattr(primitive, "_REGISTRATION_DONE", True)
    assert describe_cache.load("submit-flow") is None
    # And once full registration is in effect, store() persists as normal.
    describe_cache.store("submit-flow", {"kind": "primitive", "name": "submit-flow"})
    assert describe_cache.load("submit-flow") is not None


def test_store_noops_when_registration_attr_absent(monkeypatch):
    # A1 build-poison guard, the DEFAULT branch (mutation-triage-2026-07-17
    # Unit E). ``_full_registration_done`` reads
    # ``getattr(primitive, "_REGISTRATION_DONE", False)`` — the guard must hold
    # even when the latch attribute is ABSENT (not merely present-and-False, the
    # only case test_store_refused_under_partial_registration exercised). With
    # the attribute deleted, ``store`` still no-ops. This pins the ``default``:
    # a ``getattr(..., True)`` mutation would persist under a missing latch (a
    # false-positive registry latch poisons every full-path describe reader for
    # the build), and that mutant survived the old single-case test.
    from hpc_agent._kernel.registry import primitive

    monkeypatch.delattr(primitive, "_REGISTRATION_DONE", raising=False)
    describe_cache.store("submit-flow", {"kind": "primitive", "name": "submit-flow"})
    # Nothing was written while the latch was absent — a later full-registration
    # read still misses.
    monkeypatch.setattr(primitive, "_REGISTRATION_DONE", True, raising=False)
    assert describe_cache.load("submit-flow") is None


def test_load_rejects_non_dict_payload(monkeypatch):
    # ``load`` returns ``data if isinstance(data, dict) else None`` — a cached
    # payload that parses as valid JSON but is NOT an object (a list, here) must
    # be rejected, not returned. Pins the isinstance guard so a non-dict cached
    # blob can never reach a describe reader as a payload (memo describe_cache
    # ``load`` 3/11 survivor set).
    from hpc_agent._kernel.registry import primitive

    monkeypatch.setattr(primitive, "_REGISTRATION_DONE", True, raising=False)
    path = describe_cache._cache_path("submit-flow")
    assert path is not None  # cache is enabled under the fake clean build
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")  # a list, not a dict
    assert describe_cache.load("submit-flow") is None


def test_emit_describe_not_found_is_not_cached(monkeypatch):
    def _raise(*, name, _catalog=None):
        raise ValueError(f"no skill or primitive named {name!r}")

    monkeypatch.setattr(setup, "describe", _raise)
    rc = setup._emit_describe("nope-not-real")
    assert rc != setup.EXIT_OK
    # Nothing cached for a not-found name.
    assert describe_cache.load("nope-not-real") is None


def test_load_path_drags_no_heavy_imports(tmp_path):
    """Cold ``load`` must not pull ``importlib.metadata`` NOR ``run_record``.

    The old cache keyed on ``importlib.metadata.version()`` (the email/metadata
    ``_adapters`` chain, ~125ms) and resolved its dir via ``run_record``
    (dataclasses + inspect, ~85ms) — both paid on EVERY describe, cache hit
    included. Content-keying via ``_build_info`` + the ``_homedir`` leaf drops
    both. Subprocess-isolated so a sibling test that already imported them can't
    mask a regression. BUILD_SHA is forced set so ``load`` actually reaches the
    homedir resolver (proving the leaf module skips ``run_record`` too).
    """
    code = (
        "import sys; "
        "import hpc_agent._build_info as _b; "
        "_b.BUILD_SHA = 'deadbeef'; _b.BUILD_DIRTY = False; "
        "import hpc_agent.state.describe_cache as dc; "
        "dc.load('find'); "
        "print({"
        "'importlib.metadata._adapters': 'importlib.metadata._adapters' in sys.modules, "
        "'hpc_agent.state.run_record': 'hpc_agent.state.run_record' in sys.modules})"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
        env={**_clean_env(tmp_path)},
    )
    loaded = ast.literal_eval(proc.stdout.strip())
    offenders = sorted(name for name, present in loaded.items() if present)
    assert not offenders, (
        f"cold describe_cache.load() eagerly loaded {offenders} — content-keying "
        f"via _build_info + the _homedir leaf must keep both off the path. "
        f"stderr:\n{proc.stderr}"
    )


def _clean_env(tmp_path) -> dict[str, str]:
    import os

    env = dict(os.environ)
    env["HPC_JOURNAL_DIR"] = str(tmp_path)
    env.pop("HPC_NO_DESCRIBE_CACHE", None)
    return env
