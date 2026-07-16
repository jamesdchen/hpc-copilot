"""submit-preflight verdict TTL cache + install-commands version skip (rank 14).

``ops/_submit_preflight_cache`` caches a PASSING ``submit-preflight`` verdict per
``(cluster, framework-version, clusters.yaml mtime)`` for a TTL, mirroring the
#255 ``state.preflight_cache`` discipline (successes only, structural
invalidation, disclosed hit, env kill switch). Separately it tracks the last
version ``install-commands`` ran for so the asset copy is skipped when the wheel
stamp has not moved.

These tests pin the freshness/invalidation semantics AND that
``submit_preflight`` short-circuits the fan-out on a hit / caches only passes /
skips install-commands when the version is unchanged.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hpc_agent.ops import _submit_preflight_cache as cache


@pytest.fixture(autouse=True)
def _redirect_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path))
    monkeypatch.delenv("HPC_NO_SUBMIT_PREFLIGHT_CACHE", raising=False)
    monkeypatch.delenv("HPC_SUBMIT_PREFLIGHT_TTL_SEC", raising=False)


def _key(cluster="hoffman2", version="0.10.5", mtime="111"):
    return cache.submit_preflight_cache_key(cluster=cluster, version=version, clusters_mtime=mtime)


def _pass_verdict():
    return {
        "overall": "pass",
        "elapsed_total_sec": 3.0,
        "install_commands": {"envelope": {"ok": True}, "elapsed_sec": 1.0, "ok": True},
        "load_context": None,
        "check_preflight": None,
        "resolve_resources": None,
    }


# ── verdict cache freshness / invalidation ─────────────────────────────────


def test_miss_before_record() -> None:
    assert cache.read_fresh_verdict(_key()) is None


def test_record_then_fresh_hit_is_disclosed() -> None:
    k = _key()
    cache.record_verdict(k, _pass_verdict())
    hit = cache.read_fresh_verdict(k)
    assert hit is not None
    assert hit["overall"] == "pass"
    assert hit["cache"]["hit"] is True
    assert hit["cache"]["key"] == k
    assert "TTL cache" in hit["cache"]["message"]


def test_stored_verdict_strips_nested_cache() -> None:
    """A re-served verdict never nests a prior disclosure."""
    k = _key()
    v = _pass_verdict()
    v["cache"] = {"hit": True, "stale": "block"}
    cache.record_verdict(k, v)
    hit = cache.read_fresh_verdict(k)
    assert hit is not None and hit["cache"].get("stale") is None


def test_expired_entry_misses() -> None:
    k = _key()
    cache.record_verdict(k, _pass_verdict())
    future = datetime.now(timezone.utc) + timedelta(seconds=cache.DEFAULT_TTL_SEC + 5)
    assert cache.read_fresh_verdict(k, now=future) is None


def test_key_folds_cluster_version_mtime() -> None:
    base = _key()
    assert cache.read_fresh_verdict(base) is None
    cache.record_verdict(base, _pass_verdict())
    assert cache.read_fresh_verdict(_key(cluster="carc")) is None
    assert cache.read_fresh_verdict(_key(version="9.9.9")) is None
    assert cache.read_fresh_verdict(_key(mtime="222")) is None
    assert cache.read_fresh_verdict(base) is not None


def test_kill_switch_disables_read_and_record(monkeypatch) -> None:
    monkeypatch.setenv("HPC_NO_SUBMIT_PREFLIGHT_CACHE", "1")
    k = _key()
    cache.record_verdict(k, _pass_verdict())  # no-op
    assert cache.read_fresh_verdict(k) is None


def test_ttl_override(monkeypatch) -> None:
    monkeypatch.setenv("HPC_SUBMIT_PREFLIGHT_TTL_SEC", "1")
    k = _key()
    cache.record_verdict(k, _pass_verdict())
    future = datetime.now(timezone.utc) + timedelta(seconds=2)
    assert cache.read_fresh_verdict(k, now=future) is None


# ── install-commands version marker ────────────────────────────────────────


def test_install_marker_fresh_only_on_matching_version() -> None:
    assert cache.install_commands_fresh("1.0.0") is False
    cache.record_install_commands("1.0.0")
    assert cache.install_commands_fresh("1.0.0") is True
    assert cache.install_commands_fresh("1.0.1") is False  # version bump misses


def test_install_marker_disabled_never_fresh(monkeypatch) -> None:
    cache.record_install_commands("1.0.0")
    monkeypatch.setenv("HPC_NO_SUBMIT_PREFLIGHT_CACHE", "1")
    assert cache.install_commands_fresh("1.0.0") is False


def test_empty_version_never_fresh() -> None:
    cache.record_install_commands("")
    assert cache.install_commands_fresh("") is False


# ── submit_preflight wiring ────────────────────────────────────────────────


def test_submit_preflight_returns_cached_hit_without_fanout(monkeypatch) -> None:
    """A fresh verdict short-circuits the entire fan-out."""
    import hpc_agent as _hpc
    from hpc_agent.ops import submit_preflight as sp

    version = _hpc.__version__ or ""
    key = cache.submit_preflight_cache_key(
        cluster=None, version=version, clusters_mtime=cache.clusters_yaml_mtime_token()
    )
    cache.record_verdict(key, _pass_verdict())

    def _boom(calls, **kw):
        raise AssertionError("fan-out must not run on a cache hit")

    monkeypatch.setattr(sp, "_run_subcalls", _boom)
    out = sp.submit_preflight(experiment_dir=".", cluster=None)
    assert out["cache"]["hit"] is True


def test_submit_preflight_caches_only_passes(monkeypatch) -> None:
    """A failing verdict is never cached; a passing one is."""
    from hpc_agent.ops import submit_preflight as sp

    fail = {
        "install-commands": {"envelope": {"ok": False}, "elapsed_sec": 0.1, "ok": False},
        "load-context": {"envelope": {"ok": True}, "elapsed_sec": 0.1, "ok": True},
    }
    monkeypatch.setattr(sp, "_run_subcalls", lambda calls, **kw: fail)
    out = sp.submit_preflight(experiment_dir=".", cluster="hoffman2")
    assert out["overall"] == "fail"

    import hpc_agent as _hpc

    version = _hpc.__version__ or ""
    key = cache.submit_preflight_cache_key(
        cluster="hoffman2", version=version, clusters_mtime=cache.clusters_yaml_mtime_token()
    )
    assert cache.read_fresh_verdict(key) is None, "a fail must not be cached"


def test_submit_preflight_skips_install_commands_when_version_fresh(monkeypatch) -> None:
    """When install-commands already ran for this version, the sub-call is skipped
    (not built) even on a verdict-cache miss."""
    import hpc_agent as _hpc
    from hpc_agent.ops import submit_preflight as sp

    version = _hpc.__version__ or ""
    cache.record_install_commands(version)

    seen_skips = {}

    def _capture(*, experiment_dir, cluster, skip, resolve_kwargs=None):
        seen_skips["skip"] = list(skip)
        return []

    monkeypatch.setattr(sp, "_build_subcalls", _capture)
    monkeypatch.setattr(sp, "_run_subcalls", lambda calls, **kw: {})
    sp.submit_preflight(experiment_dir=".", cluster="hoffman2")
    assert "install-commands" in seen_skips["skip"]
