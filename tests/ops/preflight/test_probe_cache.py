"""Tests for the preflight probe verdict cache (latency: elide re-handshakes).

The cache's contract is ban-safety + honesty: SUCCESS-only, TTL-bounded,
breaker-invalidated, honest "(cached: ...)" details, fail-open. A cache hit
must issue ZERO network traffic (no tcp:22, no ssh); anything less than a
fully-passing verdict must probe live.

State isolation comes from the suite-wide autouse ``_isolated_journal_home``
fixture (the cache resolves its dir through ``run_record._current_homedir``).
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent.infra import ssh_circuit
from hpc_agent.ops.preflight import check as preflight
from hpc_agent.ops.preflight import probe_cache

if TYPE_CHECKING:
    from pathlib import Path

HOST = "h.test.edu"


def _write_clusters(tmp_path: Path) -> Path:
    p = tmp_path / "clusters.yaml"
    p.write_text(
        f"x:\n  host: {HOST}\n  user: u\n  scheduler: slurm\n  scratch: /s\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def green_local_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, list[Any]]:
    """Local checks green; count every tcp and ssh attempt. Returns the ledger."""
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.delenv(probe_cache.TTL_ENV, raising=False)
    monkeypatch.setattr(preflight, "agent_available", lambda: True)
    monkeypatch.setattr(preflight, "agent_detail", lambda: "agent ok")
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(_write_clusters(tmp_path)))
    ledger: dict[str, list[Any]] = {"tcp": [], "ssh": []}

    def _tcp(*a: Any, **k: Any) -> Any:
        ledger["tcp"].append(a)
        return contextlib.nullcontext()

    monkeypatch.setattr(preflight.socket, "create_connection", _tcp)

    from types import SimpleNamespace

    def _ssh_run(cmd: str, *, ssh_target: str, timeout: Any) -> Any:
        ledger["ssh"].append((cmd, ssh_target))
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr("hpc_agent.infra.remote.ssh_run", _ssh_run)
    return ledger


def test_second_preflight_within_ttl_issues_zero_network_traffic(
    green_local_env: dict[str, list[Any]],
) -> None:
    """The whole point: a fresh passing verdict replays with NO tcp, NO ssh —
    every elided connection is one fewer for the intrusion filter to count."""
    first = preflight.check_preflight(cluster="x")
    assert first["all_ok"] is True
    assert len(green_local_env["tcp"]) == 1
    assert len(green_local_env["ssh"]) == 1

    second = preflight.check_preflight(cluster="x")
    assert second["all_ok"] is True
    # ZERO new network traffic.
    assert len(green_local_env["tcp"]) == 1
    assert len(green_local_env["ssh"]) == 1
    # Honest: the replayed probe checks say so.
    by_name = {c["name"]: c for c in second["checks"]}
    assert "(cached: probe passed" in by_name["cluster_ssh_echo"]["detail"]
    assert "(cached: probe passed" in by_name["cluster_tcp_22"]["detail"]


def test_failed_probe_is_never_cached(
    green_local_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A red verdict must re-probe next time — only SUCCESS is reusable."""
    from types import SimpleNamespace

    def _ssh_fail(cmd: str, *, ssh_target: str, timeout: Any) -> Any:
        green_local_env["ssh"].append((cmd, ssh_target))
        return SimpleNamespace(returncode=255, stdout="", stderr="kex timeout")

    monkeypatch.setattr("hpc_agent.infra.remote.ssh_run", _ssh_fail)
    assert preflight.check_preflight(cluster="x")["all_ok"] is False
    assert preflight.check_preflight(cluster="x")["all_ok"] is False
    assert len(green_local_env["ssh"]) == 2  # probed live both times


def test_breaker_failure_newer_than_verdict_invalidates(
    green_local_env: dict[str, list[Any]],
) -> None:
    """A connection failure recorded AFTER the verdict means the path degraded
    since — the cache must not replay a green over fresh red evidence."""
    preflight.check_preflight(cluster="x")
    assert len(green_local_env["ssh"]) == 1

    ssh_circuit.record_connection_failure(HOST, detail="connect timeout")

    preflight.check_preflight(cluster="x")
    assert len(green_local_env["ssh"]) == 2  # re-probed live


def test_open_circuit_invalidates(green_local_env: dict[str, list[Any]]) -> None:
    preflight.check_preflight(cluster="x")
    for _ in range(ssh_circuit.CIRCUIT_THRESHOLD):
        ssh_circuit.record_connection_failure(HOST, detail="connect timeout")
    assert probe_cache.load_fresh(HOST, key=preflight._probe_cache_key(HOST, None)) is None


def test_ttl_zero_disables_the_cache(
    green_local_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(probe_cache.TTL_ENV, "0")
    preflight.check_preflight(cluster="x")
    preflight.check_preflight(cluster="x")
    assert len(green_local_env["ssh"]) == 2


def test_expired_verdict_probes_live() -> None:
    """Unit: an entry past the TTL is a miss (clock injected, no real time)."""
    key = "k" * 16
    checks = [{"name": "cluster_ssh_echo", "ok": True, "detail": "d"}]
    probe_cache.store(HOST, key=key, checks=checks, clock=lambda: 1000.0)
    ttl = probe_cache.probe_ttl_sec()
    assert probe_cache.load_fresh(HOST, key=key, clock=lambda: 1000.0 + ttl - 1) is not None
    assert probe_cache.load_fresh(HOST, key=key, clock=lambda: 1000.0 + ttl + 1) is None


def test_store_refuses_a_block_with_any_failure() -> None:
    key = "k" * 16
    probe_cache.store(
        HOST,
        key=key,
        checks=[
            {"name": "cluster_tcp_22", "ok": True, "detail": "open"},
            {"name": "cluster_ssh_echo", "ok": False, "detail": "kex timeout"},
        ],
        clock=lambda: 1000.0,
    )
    assert probe_cache.load_fresh(HOST, key=key, clock=lambda: 1001.0) is None


def test_echo_and_uv_probe_shapes_never_collide(
    green_local_env: dict[str, list[Any]],
) -> None:
    """An echo-only verdict says nothing about uv: a spec'd (uv) preflight
    after a bare one must probe live (distinct cache keys)."""
    preflight.check_preflight(cluster="x")
    assert len(green_local_env["ssh"]) == 1

    spec = {"job_env": {"HPC_RUNTIME": "uv"}, "ssh_target": f"u@{HOST}"}
    preflight.check_preflight(cluster="x", spec=spec)
    assert len(green_local_env["ssh"]) == 2  # cache miss: different key


def test_cache_is_fail_open_on_corrupt_state() -> None:
    """A corrupt/garbage state file degrades to 'no cache', never a raise."""
    path = probe_cache.cache_path(HOST)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    key = "k" * 16
    assert probe_cache.load_fresh(HOST, key=key) is None
    # store() replaces the corrupt doc rather than raising.
    probe_cache.store(HOST, key=key, checks=[{"name": "x", "ok": True, "detail": ""}])
    assert probe_cache.load_fresh(HOST, key=key) is not None
