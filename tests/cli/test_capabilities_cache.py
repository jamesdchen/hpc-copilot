"""Build+dist-keyed disk cache for the ``capabilities`` verb output (R4 ruling).

Mirrors ``tests/cli/test_describe_cache.py``: roundtrip, the disable/invalidation
latches, the two output-shape variants kept separate, the store-gate, and an
in-process byte-identity proof that a warm hit reproduces the cold walk exactly.
"""

from __future__ import annotations

import argparse
import io
from contextlib import redirect_stdout

import pytest

from hpc_agent import _build_info
from hpc_agent._kernel.extension.capabilities import _capabilities_handler
from hpc_agent.cli import _fast_path_cache
from hpc_agent.state import capabilities_cache


@pytest.fixture(autouse=True)
def _redirect_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path))
    monkeypatch.delenv("HPC_NO_CAPABILITIES_CACHE", raising=False)
    monkeypatch.delenv("HPC_NO_SSH_MULTIPLEX", raising=False)
    # This repo is a source checkout (BUILD_SHA is None), where the cache is
    # DISABLED by construction. Pin a clean fake build so the roundtrip/gate
    # tests exercise the ENABLED path; the trap tests below re-patch as needed.
    monkeypatch.setattr(_build_info, "BUILD_SHA", "cafef00d")
    monkeypatch.setattr(_build_info, "BUILD_DIRTY", False)


def test_store_then_load_roundtrip_bare():
    payload = {"version": "9.9.9", "subcommands": ["submit", "status"]}
    assert capabilities_cache.load("bare") is None  # miss
    capabilities_cache.store("bare", payload)
    assert capabilities_cache.load("bare") == payload


def test_store_then_load_roundtrip_full():
    text = "# hpc-agent llms-full\n\n## Catalog\n..."
    assert capabilities_cache.load("full") is None  # miss
    capabilities_cache.store("full", text)
    assert capabilities_cache.load("full") == text


def test_variants_cached_separately():
    # capabilities vs capabilities --full produce different outputs; each keys to
    # its own file so a bare hit never serves the --full dump and vice versa.
    capabilities_cache.store("bare", {"version": "9.9.9"})
    capabilities_cache.store("full", "llms-full text")
    assert capabilities_cache.load("bare") == {"version": "9.9.9"}
    assert capabilities_cache.load("full") == "llms-full text"
    # Shape mismatch (bare payload asked as full, or vice versa) is a miss, not a
    # coerced hit.
    assert isinstance(capabilities_cache.load("bare"), dict)
    assert isinstance(capabilities_cache.load("full"), str)


def test_disabled_env_is_a_miss_and_store_noop(monkeypatch):
    capabilities_cache.store("bare", {"version": "9.9.9"})
    monkeypatch.setenv("HPC_NO_CAPABILITIES_CACHE", "1")
    assert capabilities_cache.load("bare") is None
    capabilities_cache.store("bare", {"version": "other"})
    monkeypatch.delenv("HPC_NO_CAPABILITIES_CACHE")
    # The store while disabled was a no-op; the pre-disable payload survives.
    assert capabilities_cache.load("bare") == {"version": "9.9.9"}


def test_new_build_sha_at_same_version_invalidates(monkeypatch):
    # This project force-reinstalls dev wheels at the SAME package version, so a
    # version-string key would serve a STALE capabilities envelope. Keying the
    # cache dir on BUILD_SHA closes the trap — a new wheel gets a new sha even at
    # an identical version, so its payload lands in a fresh dir.
    monkeypatch.setattr(_build_info, "BUILD_SHA", "aaaaaaaa")
    capabilities_cache.store("bare", {"version": "9.9.9"})
    assert capabilities_cache.load("bare") is not None
    monkeypatch.setattr(_build_info, "BUILD_SHA", "bbbbbbbb")
    assert capabilities_cache.load("bare") is None
    # And the two shas resolve to genuinely different cache paths.
    monkeypatch.setattr(_build_info, "BUILD_SHA", "aaaaaaaa")
    path_a = capabilities_cache._cache_path("bare")
    monkeypatch.setattr(_build_info, "BUILD_SHA", "bbbbbbbb")
    path_b = capabilities_cache._cache_path("bare")
    assert path_a is not None and path_b is not None and path_a != path_b


def test_dist_signature_change_invalidates(monkeypatch):
    # A plugin install/uninstall changes the catalog WITHOUT necessarily changing
    # BUILD_SHA. The installed-dist signature captures that: a stored payload
    # under signature A must MISS once the signature flips to B.
    monkeypatch.setattr(_fast_path_cache, "installed_dist_signature", lambda: "sig-A")
    capabilities_cache.store("bare", {"version": "9.9.9"})
    assert capabilities_cache.load("bare") == {"version": "9.9.9"}
    monkeypatch.setattr(_fast_path_cache, "installed_dist_signature", lambda: "sig-B")
    assert capabilities_cache.load("bare") is None  # plugin set changed → miss


def test_ssh_multiplex_env_change_invalidates(monkeypatch):
    # The bare envelope reports ssh_multiplexing from HPC_NO_SSH_MULTIPLEX; that
    # var is folded into the signature so a hit can never serve the wrong flag.
    capabilities_cache.store("bare", {"ssh_multiplexing": True})
    assert capabilities_cache.load("bare") is not None
    monkeypatch.setenv("HPC_NO_SSH_MULTIPLEX", "1")
    assert capabilities_cache.load("bare") is None


def test_source_checkout_disables_cache(monkeypatch):
    # BUILD_SHA is None on a source checkout: no cheap content identity, so the
    # cache is disabled — load misses and store no-ops even for a valid payload.
    monkeypatch.setattr(_build_info, "BUILD_SHA", None)
    assert capabilities_cache._cache_path("bare") is None
    capabilities_cache.store("bare", {"version": "9.9.9"})
    monkeypatch.setattr(_build_info, "BUILD_SHA", "cafef00d")
    assert capabilities_cache.load("bare") is None  # nothing was written


def test_dirty_wheel_disables_cache(monkeypatch):
    # A wheel from a dirty tree: g<sha> is not content-true, so the cache is
    # disabled rather than key on it.
    monkeypatch.setattr(_build_info, "BUILD_SHA", "deadbeef")
    monkeypatch.setattr(_build_info, "BUILD_DIRTY", True)
    assert capabilities_cache._cache_path("bare") is None
    capabilities_cache.store("bare", {"version": "9.9.9"})
    monkeypatch.setattr(_build_info, "BUILD_DIRTY", False)
    assert capabilities_cache.load("bare") is None  # store was a no-op while dirty


def test_unknown_variant_never_caches():
    assert capabilities_cache._cache_path("sideways") is None
    capabilities_cache.store("sideways", {"version": "9.9.9"})  # type: ignore[arg-type]
    assert capabilities_cache.load("sideways") is None


def test_store_refused_under_partial_registration(monkeypatch):
    # A1 poisoning guard: the payload projects the WHOLE catalog and is whole-
    # truth only against the fully-walked registry. store() must refuse to
    # persist under a partial registry (the single-verb fast-path latch).
    from hpc_agent._kernel.registry import primitive

    monkeypatch.setattr(primitive, "_REGISTRATION_DONE", False)
    capabilities_cache.store("bare", {"version": "9.9.9"})
    monkeypatch.setattr(primitive, "_REGISTRATION_DONE", True)
    assert capabilities_cache.load("bare") is None  # nothing persisted
    capabilities_cache.store("bare", {"version": "9.9.9"})
    assert capabilities_cache.load("bare") is not None


def _run_handler(*, full: bool) -> str:
    """Invoke the handler in-process, returning its full stdout."""
    args = argparse.Namespace(full=full)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = _capabilities_handler(args)
    assert rc == 0
    return buf.getvalue()


def test_byte_identity_cold_walk_equals_warm_hit_bare():
    # The cache promise: a warm hit is BYTE-IDENTICAL to the cold walk. First call
    # misses (walk → store → emit); second serves from cache through the same
    # envelope path. Under the fake clean build + tmp journal home, the cache is
    # enabled, so the second call is a genuine hit.
    cold = _run_handler(full=False)
    assert capabilities_cache.load("bare") is not None  # the walk populated it
    warm = _run_handler(full=False)
    assert cold == warm and cold.strip()


def test_byte_identity_cold_walk_equals_warm_hit_full():
    cold = _run_handler(full=True)
    assert capabilities_cache.load("full") is not None
    warm = _run_handler(full=True)
    assert cold == warm and cold.strip()


def test_disabled_cache_is_pure_passthrough(monkeypatch):
    # On a source checkout (cache disabled) the handler output must be UNCHANGED
    # from the no-cache path: two runs both take the live walk and match.
    monkeypatch.setattr(_build_info, "BUILD_SHA", None)
    first = _run_handler(full=False)
    assert capabilities_cache.load("bare") is None  # nothing cached while disabled
    second = _run_handler(full=False)
    assert first == second and first.strip()
