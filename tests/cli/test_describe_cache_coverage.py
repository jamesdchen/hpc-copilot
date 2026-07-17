"""Behaviour-pinning coverage for :mod:`hpc_agent.state.describe_cache`.

The 2026-07-17 mutation triage-2 (``docs/plans/mutation-triage-2-2026-07-17.md``)
found describe-cache among the curated modules that ran DARK — it aborted the
mutation baseline outright (Finding #1b), so NOTHING confirmed which surviving
boundary/operator/constant mutants the suite kills. describe-cache is a
build-content-keyed disk cache: a silent mutation in its KEY derivation serves a
STALE describe payload (the "same-version-reinstall" trap the module was written
to close), and a silent mutation in its layout literals or error handling either
corrupts every reader or raises on a cache the module promises is opportunistic.

``tests/cli/test_describe_cache.py`` already pins the disable/roundtrip/partial-
registry (A1 build-poison) paths. This file ADDS the pins those tests leave a
mutant free to survive because they exercise store()/load() SYMMETRICALLY (a
mutated-but-self-consistent key or layout literal round-trips undetected): the
``g<BUILD_SHA>`` key format, the exact on-disk layout literals, the non-JSON
(``ValueError``) read fallback, and the ``exist_ok`` re-store branch.

Every assertion notes the mutation it kills inline.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from hpc_agent import _build_info
from hpc_agent.state import describe_cache

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _enabled_clean_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the fully-ENABLED cache path so key/layout/store mutants are reachable.

    This repo is a source checkout (``BUILD_SHA is None``), where the cache is
    DISABLED by construction — so a fake clean build is required to exercise the
    stored-payload branches at all. ``_REGISTRATION_DONE`` is forced True so
    ``store`` is not no-op'd by the A1 partial-registry guard (which the sibling
    file pins directly).
    """
    from hpc_agent._kernel.registry import primitive

    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path))
    monkeypatch.delenv("HPC_NO_DESCRIBE_CACHE", raising=False)
    monkeypatch.setattr(_build_info, "BUILD_SHA", "cafef00d")
    monkeypatch.setattr(_build_info, "BUILD_DIRTY", False)
    monkeypatch.setattr(primitive, "_REGISTRATION_DONE", True, raising=False)


# ── _content_key: the g-prefixed BUILD_SHA (a wrong key = stale-serve) ─────────


def test_content_key_is_the_g_prefixed_build_sha() -> None:
    # kills: the ``f"g{BUILD_SHA}"`` literal — the documented on-disk dir name is
    # ``g<BUILD_SHA>`` (state/describe_cache.py docstring). The existing
    # invalidation test only checks that two shas differ, so a mutated prefix
    # (``XXgXX...``) still produces distinct-per-sha dirs and survives it; this
    # pins the EXACT format so the dir a stranger's tooling globs stays stable.
    assert describe_cache._content_key() == "gcafef00d"


def test_content_key_tracks_the_build_sha_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    # kills: any mutation that drops/reshapes the sha inside the key — the whole
    # sha must ride into the key, else a same-version reinstall (new sha) could
    # collide with the old dir and serve a stale payload.
    monkeypatch.setattr(_build_info, "BUILD_SHA", "0badf00d")
    assert describe_cache._content_key() == "g0badf00d"


# ── _cache_path: the exact documented on-disk layout ───────────────────────────


def test_cache_path_layout_is_describe_cache_key_name_json() -> None:
    # kills: the ``"describe_cache"`` dir literal and the ``f"{name}.json"``
    # suffix. store() and load() BOTH build the path via _cache_path, so a
    # mutated-but-self-consistent literal round-trips undetected by the existing
    # roundtrip test; pin the tail so the documented
    # ``describe_cache/g<sha>/<name>.json`` layout can't silently drift.
    path = describe_cache._cache_path("submit-flow")
    assert path is not None
    assert path.parts[-3:] == ("describe_cache", "gcafef00d", "submit-flow.json")


# ── load: the non-JSON (ValueError) read fallback ──────────────────────────────


def test_load_returns_none_on_corrupt_non_json_file() -> None:
    # kills: dropping ``ValueError`` from ``except (OSError, ValueError)`` in
    # load(). The existing non-dict test writes VALID json (a list, caught by the
    # isinstance guard); nothing writes a byte-corrupt file, so a mutant that
    # narrows the except to bare ``OSError`` would let json.load raise straight
    # through the opportunistic cache. A hit must never raise.
    path = describe_cache._cache_path("submit-flow")
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    assert describe_cache.load("submit-flow") is None


# ── store: the exist_ok re-store branch ────────────────────────────────────────


def test_store_writes_a_second_name_into_the_existing_build_dir() -> None:
    # kills: ``mkdir(parents=True, exist_ok=True)`` → ``exist_ok=False``. The
    # existing tests only ever land ONE payload per build dir, so a mutant that
    # drops exist_ok survives — the FIRST store creates the dir, and the second
    # store into the SAME build dir would raise FileExistsError (swallowed),
    # silently losing every describe after the first for a build's lifetime.
    describe_cache.store("submit-flow", {"kind": "primitive", "name": "submit-flow"})
    describe_cache.store("find", {"kind": "primitive", "name": "find"})
    assert describe_cache.load("submit-flow") == {"kind": "primitive", "name": "submit-flow"}
    assert describe_cache.load("find") == {"kind": "primitive", "name": "find"}


def test_store_then_load_is_a_hit_only_for_the_stored_name() -> None:
    # kills: a store/load key mutation that widens or narrows the per-NAME file
    # (e.g. dropping the name from the path) — a stored name hits, an unstored
    # sibling under the same build misses. Pins the name boundary of the key.
    describe_cache.store("submit-flow", {"kind": "primitive", "name": "submit-flow"})
    assert describe_cache.load("submit-flow") is not None
    assert describe_cache.load("aggregate-flow") is None


def test_load_of_a_json_object_is_returned_verbatim() -> None:
    # kills: the ``data if isinstance(data, dict) else None`` guard flipping to
    # reject dicts — a well-formed cached OBJECT must survive as the payload
    # (the hit half of the boundary the non-dict test only pins from the reject
    # side). Written directly so no store()-side mutation can mask it.
    payload = {"kind": "primitive", "name": "submit-flow", "content": {"verb": "workflow"}}
    path = describe_cache._cache_path("submit-flow")
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert describe_cache.load("submit-flow") == payload
