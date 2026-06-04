"""TTL preflight cache + its submit-flow wiring (#255).

The cache skips the cluster-side ``command -v uv`` round-trip when the same
``(host, env-activation, framework-version)`` was validated within the TTL.
These tests pin the freshness/invalidation semantics and assert submit-flow
consults the cache before paying the SSH probe.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hpc_agent.state import preflight_cache


@pytest.fixture(autouse=True)
def _redirect_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path))
    monkeypatch.delenv("HPC_NO_PREFLIGHT_CACHE", raising=False)
    monkeypatch.delenv("HPC_PREFLIGHT_TTL_SEC", raising=False)


def _key(activation="m|s|env", version="0.10.5"):
    return preflight_cache.preflight_cache_key(
        host="hoffman2", activation=activation, version=version
    )


def test_key_changes_with_activation_and_version():
    base = _key()
    assert base != _key(activation="m2|s|env")  # conda env / modules edit
    assert base != _key(version="0.10.6")  # framework bump
    assert base == _key()  # stable for identical inputs


def test_miss_then_hit_within_ttl():
    key = _key()
    assert preflight_cache.is_preflight_fresh(key) is False  # nothing recorded yet
    preflight_cache.record_preflight(key, checks=["uv_present"])
    assert preflight_cache.is_preflight_fresh(key) is True


def test_expired_entry_is_not_fresh():
    key = _key()
    preflight_cache.record_preflight(key)
    # Look at the entry from far in the future — past the default TTL.
    future = datetime.now(timezone.utc) + timedelta(seconds=preflight_cache.DEFAULT_TTL_SEC + 1)
    assert preflight_cache.is_preflight_fresh(key, now=future) is False


def test_ttl_override_env(monkeypatch):
    monkeypatch.setenv("HPC_PREFLIGHT_TTL_SEC", "5")
    key = _key()
    preflight_cache.record_preflight(key)
    now = datetime.now(timezone.utc)
    assert preflight_cache.is_preflight_fresh(key, now=now + timedelta(seconds=3)) is True
    assert preflight_cache.is_preflight_fresh(key, now=now + timedelta(seconds=8)) is False


def test_disable_env_forces_miss(monkeypatch):
    key = _key()
    preflight_cache.record_preflight(key)
    assert preflight_cache.is_preflight_fresh(key) is True
    monkeypatch.setenv("HPC_NO_PREFLIGHT_CACHE", "1")
    # Disabled: never fresh (re-run), and record is a no-op.
    assert preflight_cache.is_preflight_fresh(key) is False
    preflight_cache.record_preflight(_key(activation="other"))
    monkeypatch.delenv("HPC_NO_PREFLIGHT_CACHE")
    assert preflight_cache.is_preflight_fresh(_key(activation="other")) is False


def test_corrupt_cache_file_is_a_miss(tmp_path):
    # A torn / non-JSON cache must collapse to "re-run", never raise.
    path = preflight_cache._cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    assert preflight_cache.is_preflight_fresh(_key()) is False


# --- submit-flow wiring -----------------------------------------------------


def test_submit_flow_skips_runtime_check_when_cache_fresh(monkeypatch):
    """A fresh cache entry means the cluster-side uv probe is NOT issued."""
    from hpc_agent.ops import submit_flow as sf

    calls: list[str] = []

    def _spy_runtime_check(ssh_target, *, job_env, skip):
        calls.append(ssh_target)

    monkeypatch.setattr(sf, "_preflight_runtime_check", _spy_runtime_check)

    # Pre-seed a fresh entry for the key submit-flow will compute.
    activation = "|".join(("modA", "/c/etc/profile.d/conda.sh", "envX"))
    key = preflight_cache.preflight_cache_key(
        host="u@hoffman2", activation=activation, version=_ver()
    )
    preflight_cache.record_preflight(key, checks=["uv_present"])

    job_env = {
        "HPC_RUNTIME": "uv",
        "MODULES": "modA",
        "CONDA_SOURCE": "/c/etc/profile.d/conda.sh",
        "CONDA_ENV": "envX",
    }
    sf._run_uv_preflight_for_batch(
        ssh_target="u@hoffman2",
        job_envs=[job_env],
        skip_preflight=False,
    )
    assert calls == [], "cache was fresh; runtime check should have been skipped"


def test_submit_flow_runs_and_records_on_miss(monkeypatch):
    from hpc_agent.ops import submit_flow as sf

    calls: list[str] = []
    monkeypatch.setattr(
        sf,
        "_preflight_runtime_check",
        lambda ssh_target, *, job_env, skip: calls.append(ssh_target),
    )

    job_env = {
        "HPC_RUNTIME": "uv",
        "MODULES": "modB",
        "CONDA_SOURCE": "/c/conda.sh",
        "CONDA_ENV": "envY",
    }
    sf._run_uv_preflight_for_batch(ssh_target="u@host2", job_envs=[job_env], skip_preflight=False)
    # First time: probe runs once, and the success is now cached.
    assert calls == ["u@host2"]
    activation = "|".join(("modB", "/c/conda.sh", "envY"))
    key = preflight_cache.preflight_cache_key(host="u@host2", activation=activation, version=_ver())
    assert preflight_cache.is_preflight_fresh(key) is True

    # Second time within TTL: no further probe.
    sf._run_uv_preflight_for_batch(ssh_target="u@host2", job_envs=[job_env], skip_preflight=False)
    assert calls == ["u@host2"]


def _ver() -> str:
    from hpc_agent import __version__

    return __version__ or ""
