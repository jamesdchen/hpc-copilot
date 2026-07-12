"""Tests for the cross-process ClusterSnapshot cache (elide re-inspection).

Contract mirrors ``ops/preflight/probe_cache.py``: keyed
``(cluster_name, scheduler)``, SUCCESS-only, TTL-bounded, bypassable,
fail-open. The in-process ``infra.inspect._CACHE`` short-circuits within one
process; THIS cache serves a SEPARATE process that inspected the same cluster
seconds ago. The cross-process tests simulate the second process by clearing
``_CACHE`` between two ``inspect_cluster`` calls — a disk hit must then issue
ZERO runner traffic.

State isolation comes from the suite-wide autouse ``_isolated_journal_home``
fixture (the cache resolves its dir through ``run_record._current_homedir``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent.infra import inspect as ins
from hpc_agent.state import snapshot_cache

if TYPE_CHECKING:
    from pathlib import Path

CLUSTER = "discovery"


# --- module-level unit tests (injected clock, direct store/load) ----------


def _snapshot(cluster: str = CLUSTER, scheduler: str = "slurm") -> dict[str, Any]:
    """A minimal SUCCESSFUL snapshot dict (no ``errors``)."""
    return {
        "cluster": cluster,
        "scheduler_kind": scheduler,
        "now_iso": "2026-07-12T00:00:00Z",
        "nodes": [{"name": "n1", "state": "IDLE"}],
        "errors": [],
        "parallel_environments": [],
    }


def test_expired_entry_is_a_miss() -> None:
    """An entry past the TTL is a miss (clock injected, no real time)."""
    snapshot_cache.store(CLUSTER, scheduler="slurm", snapshot=_snapshot(), clock=lambda: 1000.0)
    ttl = snapshot_cache.snapshot_ttl_sec()
    fresh = snapshot_cache.load_fresh(
        CLUSTER, scheduler="slurm", clock=lambda: 1000.0 + ttl - 1
    )
    assert fresh is not None
    assert (
        snapshot_cache.load_fresh(CLUSTER, scheduler="slurm", clock=lambda: 1000.0 + ttl + 1)
        is None
    )


def test_scheduler_is_part_of_the_key() -> None:
    """Two schedulers on one cluster occupy distinct entries (no collision)."""
    snapshot_cache.store(
        CLUSTER, scheduler="slurm", snapshot=_snapshot(scheduler="slurm"), clock=lambda: 1000.0
    )
    snapshot_cache.store(
        CLUSTER, scheduler="sge", snapshot=_snapshot(scheduler="sge"), clock=lambda: 1000.0
    )
    slurm_hit = snapshot_cache.load_fresh(CLUSTER, scheduler="slurm", clock=lambda: 1001.0)
    sge_hit = snapshot_cache.load_fresh(CLUSTER, scheduler="sge", clock=lambda: 1001.0)
    assert slurm_hit is not None and slurm_hit["scheduler_kind"] == "slurm"
    assert sge_hit is not None and sge_hit["scheduler_kind"] == "sge"
    # A scheduler that was never stored is still a miss.
    assert snapshot_cache.load_fresh(CLUSTER, scheduler="pbs", clock=lambda: 1001.0) is None


def test_degraded_snapshot_is_never_cached() -> None:
    """SUCCESS-only: a snapshot carrying any ``errors`` entry must not store."""
    degraded = _snapshot()
    degraded["errors"] = [{"cmd": "scontrol", "detail": "connection refused"}]
    snapshot_cache.store(CLUSTER, scheduler="slurm", snapshot=degraded, clock=lambda: 1000.0)
    assert snapshot_cache.load_fresh(CLUSTER, scheduler="slurm", clock=lambda: 1001.0) is None


def test_ttl_zero_disables_the_cache() -> None:
    with _env({snapshot_cache.TTL_ENV: "0"}):
        snapshot_cache.store(CLUSTER, scheduler="slurm", snapshot=_snapshot())
        assert snapshot_cache.load_fresh(CLUSTER, scheduler="slurm") is None


def test_ttl_env_overrides_the_window() -> None:
    with _env({snapshot_cache.TTL_ENV: "5"}):
        assert snapshot_cache.snapshot_ttl_sec() == 5.0
        snapshot_cache.store(CLUSTER, scheduler="slurm", snapshot=_snapshot(), clock=lambda: 0.0)
        assert snapshot_cache.load_fresh(CLUSTER, scheduler="slurm", clock=lambda: 4.0) is not None
        assert snapshot_cache.load_fresh(CLUSTER, scheduler="slurm", clock=lambda: 6.0) is None


def test_bypass_env_disables_read_and_write() -> None:
    """HPC_NO_SNAPSHOT_CACHE=1 makes store a no-op and load always a miss."""
    with _env({snapshot_cache.BYPASS_ENV: "1"}):
        snapshot_cache.store(CLUSTER, scheduler="slurm", snapshot=_snapshot())
        assert snapshot_cache.load_fresh(CLUSTER, scheduler="slurm") is None
        assert not snapshot_cache.cache_path(CLUSTER).exists()
    # With bypass lifted a fresh store/load round-trips normally.
    snapshot_cache.store(CLUSTER, scheduler="slurm", snapshot=_snapshot(), clock=lambda: 1000.0)
    assert snapshot_cache.load_fresh(CLUSTER, scheduler="slurm", clock=lambda: 1001.0) is not None


def test_corrupt_file_is_fail_open() -> None:
    """A garbage state file degrades to 'no cache', never a raise; store heals it."""
    path = snapshot_cache.cache_path(CLUSTER)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert snapshot_cache.load_fresh(CLUSTER, scheduler="slurm") is None
    # store() replaces the corrupt doc rather than raising.
    snapshot_cache.store(CLUSTER, scheduler="slurm", snapshot=_snapshot(), clock=lambda: 1000.0)
    assert snapshot_cache.load_fresh(CLUSTER, scheduler="slurm", clock=lambda: 1001.0) is not None


# --- integration through inspect_cluster (cross-process simulation) --------

_SCONTROL_FIXTURE = """\
NodeName=d11-03 Arch=x86_64 CoresPerSocket=16
   CPUAlloc=24 CPUTot=32 CPULoad=22.10
   Gres=gpu:v100:2
   GresUsed=gpu:v100:1
   RealMemory=192000 AllocMem=64000 FreeMem=120000
   State=MIXED ThreadsPerCore=1
"""


class _FakeRunner:
    def __init__(self, responses: dict[str, tuple[int, str, str]]):
        self._responses = responses
        self.calls: list[str] = []

    def run(self, cmd: str) -> tuple[int, str, str]:
        self.calls.append(cmd)
        for needle, response in self._responses.items():
            if cmd.startswith(needle):
                return response
        return 0, "", ""


def _slurm_combined(node_out: str, node_rc: int) -> str:
    return (
        f"__HPC_SCONTROL_NODE__\n{node_out}\n__HPC_SCONTROL_NODE_RC__={node_rc}\n"
        f"__HPC_SCONTROL_PART__\n\n__HPC_SCONTROL_PART_RC__=0\n"
    )


def _write_clusters(tmp_path: Path, scheduler: str = "slurm") -> Path:
    p = tmp_path / "clusters.yaml"
    p.write_text(
        "discovery:\n"
        "  host: example.invalid\n"
        "  user: tester\n"
        f"  scheduler: {scheduler}\n"
        "  scratch: /tmp\n"
        "  gpu_types: [v100, a100]\n",
        encoding="utf-8",
    )
    return p


def _green_runner() -> _FakeRunner:
    return _FakeRunner(
        {
            "echo __HPC_SCONTROL_NODE__": (0, _slurm_combined(_SCONTROL_FIXTURE, 0), ""),
            "sacct": (0, "", ""),
        }
    )


def test_write_after_successful_fetch_populates_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(_write_clusters(tmp_path)))
    ins._CACHE.clear()
    assert not snapshot_cache.cache_path(CLUSTER).exists()
    snap = ins.inspect_cluster(CLUSTER, runner=_green_runner(), use_cache=True)
    assert snap.errors == []
    # A successful fetch wrote a slurm-keyed entry to disk.
    assert snapshot_cache.load_fresh(CLUSTER, scheduler="slurm") is not None


def test_cross_process_hit_within_ttl_elides_reinspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second process (fresh _CACHE) serves from disk with ZERO runner calls."""
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(_write_clusters(tmp_path)))
    ins._CACHE.clear()
    first_runner = _green_runner()
    first = ins.inspect_cluster(CLUSTER, runner=first_runner, use_cache=True)
    assert len(first_runner.calls) >= 1  # inspected live

    # Simulate a separate process: drop the in-process cache.
    ins._CACHE.clear()
    second_runner = _green_runner()
    second = ins.inspect_cluster(CLUSTER, runner=second_runner, use_cache=True)
    # Disk hit — the runner was never touched.
    assert len(second_runner.calls) == 0
    assert {n.name for n in second.nodes} == {n.name for n in first.nodes}


def test_bypass_env_forces_reinspection_cross_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(_write_clusters(tmp_path)))
    monkeypatch.setenv(snapshot_cache.BYPASS_ENV, "1")
    ins._CACHE.clear()
    first_runner = _green_runner()
    ins.inspect_cluster(CLUSTER, runner=first_runner, use_cache=True)
    assert not snapshot_cache.cache_path(CLUSTER).exists()

    ins._CACHE.clear()
    second_runner = _green_runner()
    ins.inspect_cluster(CLUSTER, runner=second_runner, use_cache=True)
    # No disk cache to serve it — re-inspected live (same traffic as the first).
    assert len(second_runner.calls) == len(first_runner.calls) >= 1


def test_degraded_fetch_is_reinspected_cross_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A snapshot with scheduler errors is not cached; the next process re-inspects."""
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(_write_clusters(tmp_path)))
    ins._CACHE.clear()
    # rc=1 on the node section produces a snapshot carrying an errors entry.
    bad_runner = _FakeRunner(
        {"echo __HPC_SCONTROL_NODE__": (0, _slurm_combined("", 1), "")}
    )
    first = ins.inspect_cluster(CLUSTER, runner=bad_runner, use_cache=True)
    assert first.errors  # degraded
    assert not snapshot_cache.cache_path(CLUSTER).exists()

    ins._CACHE.clear()
    second_runner = _FakeRunner(
        {"echo __HPC_SCONTROL_NODE__": (0, _slurm_combined("", 1), "")}
    )
    ins.inspect_cluster(CLUSTER, runner=second_runner, use_cache=True)
    assert len(second_runner.calls) >= 1  # re-inspected live


# --- helpers --------------------------------------------------------------


class _env:
    """Context manager setting/removing env vars, restoring prior values."""

    def __init__(self, mapping: dict[str, str]):
        self._mapping = mapping
        self._saved: dict[str, str | None] = {}

    def __enter__(self) -> None:
        import os

        for k, v in self._mapping.items():
            self._saved[k] = os.environ.get(k)
            os.environ[k] = v

    def __exit__(self, *exc: object) -> None:
        import os

        for k, prior in self._saved.items():
            if prior is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prior
