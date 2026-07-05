"""Canary auto-skip: tiny-batch threshold (#263) + cached-cmd_sha TTL (#249)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec
from hpc_agent.ops import submit_flow as sf
from hpc_agent.state import canary_cache


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path))
    for var in (
        "HPC_CANARY_SKIP_THRESHOLD",
        "HPC_NO_CANARY_SKIP",
        "HPC_CANARY_TTL_SEC",
        "HPC_AGENT_ALWAYS_CANARY",
    ):
        monkeypatch.delenv(var, raising=False)


def _spec(**over):
    base = dict(
        profile="p",
        cluster="c",
        ssh_target="u@h",
        remote_path="/r",
        job_name="j",
        run_id="run-1",
        total_tasks=100,
        backend="sge",
        script=".hpc/templates/cpu_array.sh",
        job_env={"EXECUTOR": "python3 .hpc/_hpc_dispatch.py", "HPC_CMD_SHA": "sha-abc"},
        canary=True,
    )
    base.update(over)
    return SubmitFlowSpec(**base)


# --- #263 tiny-batch threshold ----------------------------------------------


def test_canary_false_never_runs():
    assert sf._should_run_canary(_spec(canary=False, total_tasks=1000)) is False


def test_large_batch_runs_canary():
    assert sf._should_run_canary(_spec(total_tasks=100)) is True


def test_tiny_batch_skips_canary_default_threshold():
    # total_tasks <= 4 (default) → skip.
    assert sf._should_run_canary(_spec(total_tasks=4)) is False
    assert sf._should_run_canary(_spec(total_tasks=5)) is True


def test_threshold_spec_field():
    assert sf._should_run_canary(_spec(total_tasks=8, canary_skip_threshold=8)) is False
    assert sf._should_run_canary(_spec(total_tasks=9, canary_skip_threshold=8)) is True


def test_threshold_env_overrides_spec(monkeypatch):
    monkeypatch.setenv("HPC_CANARY_SKIP_THRESHOLD", "0")
    # env 0 → never auto-skip even on a 1-task batch.
    assert sf._should_run_canary(_spec(total_tasks=1, canary_skip_threshold=4)) is True


def test_force_canary_overrides_tiny_batch():
    assert sf._should_run_canary(_spec(total_tasks=1, force_canary=True)) is True


def test_canary_only_always_runs_even_tiny():
    # The explicit two-phase gate is never auto-skipped.
    assert sf._should_run_canary(_spec(total_tasks=1, canary_only=True)) is True


# --- #283 operator always-canary override ------------------------------------


def test_always_canary_env_wins_over_agent_opt_out(monkeypatch):
    # The operator override beats the agent-supplied canary=false — and there
    # is no spec field that can express it (env-only by the #155/#275 motto).
    monkeypatch.setenv("HPC_AGENT_ALWAYS_CANARY", "1")
    assert sf._should_run_canary(_spec(canary=False, total_tasks=1000)) is True
    assert "always_canary" not in SubmitFlowSpec.model_fields


def test_always_canary_env_wins_over_tiny_batch_skip(monkeypatch):
    monkeypatch.setenv("HPC_AGENT_ALWAYS_CANARY", "true")
    assert sf._should_run_canary(_spec(total_tasks=1)) is True


def test_always_canary_env_wins_over_cached_cmd_sha(monkeypatch):
    from hpc_agent import __version__ as ver

    canary_cache.record_canary_validated(
        canary_cache.canary_cache_key(cmd_sha="sha-abc", version=ver or "", cluster="c")
    )
    monkeypatch.setenv("HPC_AGENT_ALWAYS_CANARY", "1")
    assert sf._should_run_canary(_spec(total_tasks=100)) is True


def test_always_canary_off_value_changes_nothing(monkeypatch):
    monkeypatch.setenv("HPC_AGENT_ALWAYS_CANARY", "0")
    assert sf._should_run_canary(_spec(canary=False, total_tasks=1000)) is False
    assert sf._should_run_canary(_spec(total_tasks=4)) is False


# --- #249 cached cmd_sha TTL ------------------------------------------------


def test_cached_cmd_sha_skips_canary(monkeypatch):
    from hpc_agent import __version__ as ver

    key = canary_cache.canary_cache_key(cmd_sha="sha-abc", version=ver or "", cluster="c")
    canary_cache.record_canary_validated(key)
    # total_tasks large (no #263 skip), but cmd_sha is fresh → #249 skip.
    assert sf._should_run_canary(_spec(total_tasks=100)) is False


def test_uncached_cmd_sha_runs_canary():
    assert sf._should_run_canary(_spec(total_tasks=100)) is True


def test_no_canary_skip_env_disables_249(monkeypatch):
    from hpc_agent import __version__ as ver

    canary_cache.record_canary_validated(
        canary_cache.canary_cache_key(cmd_sha="sha-abc", version=ver or "", cluster="c")
    )
    monkeypatch.setenv("HPC_NO_CANARY_SKIP", "1")
    assert sf._should_run_canary(_spec(total_tasks=100)) is True


def test_force_canary_overrides_cached_cmd_sha():
    from hpc_agent import __version__ as ver

    canary_cache.record_canary_validated(
        canary_cache.canary_cache_key(cmd_sha="sha-abc", version=ver or "", cluster="c")
    )
    assert sf._should_run_canary(_spec(total_tasks=100, force_canary=True)) is True


# --- canary_cache freshness semantics ---------------------------------------


def test_cache_ttl_expiry():
    key = canary_cache.canary_cache_key(cmd_sha="x", version="1", cluster="c")
    canary_cache.record_canary_validated(key)
    now = datetime.now(timezone.utc)
    assert canary_cache.is_canary_validated_fresh(key, now=now + timedelta(hours=1)) is True
    future = now + timedelta(seconds=canary_cache.DEFAULT_TTL_SEC + 1)
    assert canary_cache.is_canary_validated_fresh(key, now=future) is False


def test_cache_version_keying():
    canary_cache.record_canary_validated(
        canary_cache.canary_cache_key(cmd_sha="x", version="1", cluster="c")
    )
    # A different framework version is a different key → miss.
    assert (
        canary_cache.is_canary_validated_fresh(
            canary_cache.canary_cache_key(cmd_sha="x", version="2", cluster="c")
        )
        is False
    )


def test_cache_cluster_keying():
    """Proving run #5: cluster joined the key — a canary validated on cluster A
    must NOT let a submit on cluster B skip its own canary (modules / activation
    / scheduler dialect are cluster-local, so the proof does not transfer)."""
    canary_cache.record_canary_validated(
        canary_cache.canary_cache_key(cmd_sha="x", version="1", cluster="discovery")
    )
    # Same cmd_sha + version, DIFFERENT cluster → miss (run the canary).
    assert (
        canary_cache.is_canary_validated_fresh(
            canary_cache.canary_cache_key(cmd_sha="x", version="1", cluster="hoffman2")
        )
        is False
    )
    # The recording cluster still validates.
    assert (
        canary_cache.is_canary_validated_fresh(
            canary_cache.canary_cache_key(cmd_sha="x", version="1", cluster="discovery")
        )
        is True
    )


def test_sequential_records_preserve_both_entries():
    """Read-modify-write is locked (no lost update): two sequential records with
    DIFFERENT keys must BOTH persist (regression for the unlocked-RMW lost-update
    that clobbered one entry when two submits validated different cmd_shas)."""
    key_a = canary_cache.canary_cache_key(cmd_sha="A", version="1", cluster="c")
    key_b = canary_cache.canary_cache_key(cmd_sha="B", version="1", cluster="c")
    canary_cache.record_canary_validated(key_a)
    canary_cache.record_canary_validated(key_b)
    # The second write read the first's entry under the lock, so neither is lost.
    assert canary_cache.is_canary_validated_fresh(key_a) is True
    assert canary_cache.is_canary_validated_fresh(key_b) is True


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
    canary_cache.record_canary_validated(
        canary_cache.canary_cache_key(cmd_sha="z", version="1", cluster="c")
    )
    assert len(calls) == 1
    assert calls[0].endswith(".lock")
