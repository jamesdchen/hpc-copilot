"""The array-cap helpers in submit-flow (#339).

The cap helpers (``_cluster_array_cap`` / ``_effective_array_cap``) read the
smaller of the backend's platform cap (GitHub Actions = 256) and a cluster's
declared ``constraints.max_array_size``.

Increment 3 REPURPOSES the increment-1 hard-reject: an over-cap sweep is now
SPLIT into concurrency-bounded waves (``submit_plan``), so ``_enforce_array_cap``
no longer rejects every over-cap sweep — it fail-louds ONLY the shapes the wave
path cannot rescue (a single indivisible MPI job, or a backend that declares
``can_wave=False``). A backend with no cap (the SSH families) and a cluster that
declares no limit leave ≤cap sweeps byte-for-byte unaffected.
"""

from __future__ import annotations

import pytest

from hpc_agent import errors
from hpc_agent.ops.submit_flow import (
    _cluster_array_cap,
    _effective_array_cap,
    _enforce_array_cap,
)


def _backend(cap, *, can_wave=True):  # type: ignore[no-untyped-def]
    """Minimal stand-in whose *class* carries ``max_array_size`` / ``can_wave``.

    The guard reads both off ``type(backend)`` (capabilities, not per-run
    state), so the attributes must live on the class, not the instance.
    """
    return type("FakeBackend", (), {"max_array_size": cap, "can_wave": can_wave})()


# --------------------------------------------------------------------------- #
# _cluster_array_cap — only a *declared* limit counts.
# --------------------------------------------------------------------------- #


def test_cluster_cap_none_when_cluster_falsy() -> None:
    assert _cluster_array_cap(None) is None
    assert _cluster_array_cap("") is None


def test_cluster_cap_reads_declared_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "hpc_agent.infra.clusters.load_clusters_config",
        lambda: {"hpc1": {"constraints": {"max_array_size": 100}}},
    )
    assert _cluster_array_cap("hpc1") == 100


def test_cluster_cap_none_when_no_constraints_block(monkeypatch: pytest.MonkeyPatch) -> None:
    # A cluster that declares no constraints must NOT synthesise the
    # ClusterConstraints default (1000) — today's behaviour is unbounded.
    monkeypatch.setattr(
        "hpc_agent.infra.clusters.load_clusters_config",
        lambda: {"hpc1": {"scheduler": "slurm"}},
    )
    assert _cluster_array_cap("hpc1") is None


def test_cluster_cap_none_when_unknown_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "hpc_agent.infra.clusters.load_clusters_config",
        lambda: {"other": {"constraints": {"max_array_size": 100}}},
    )
    assert _cluster_array_cap("hpc1") is None


def test_cluster_cap_swallows_load_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> dict:
        raise RuntimeError("no clusters.yaml here")

    monkeypatch.setattr("hpc_agent.infra.clusters.load_clusters_config", _boom)
    assert _cluster_array_cap("hpc1") is None


def test_cluster_cap_ignores_bool(monkeypatch: pytest.MonkeyPatch) -> None:
    # ``True`` is an int subclass; a stray bool must not be read as a cap of 1.
    monkeypatch.setattr(
        "hpc_agent.infra.clusters.load_clusters_config",
        lambda: {"hpc1": {"constraints": {"max_array_size": True}}},
    )
    assert _cluster_array_cap("hpc1") is None


# --------------------------------------------------------------------------- #
# _effective_array_cap — reconcile backend + cluster, smaller wins.
# --------------------------------------------------------------------------- #


def test_effective_cap_none_when_both_unbounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hpc_agent.infra.clusters.load_clusters_config", dict)
    assert _effective_array_cap(_backend(None), "hpc1") is None


def test_effective_cap_backend_only() -> None:
    assert _effective_array_cap(_backend(256), None) == 256


def test_effective_cap_smaller_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "hpc_agent.infra.clusters.load_clusters_config",
        lambda: {"hpc1": {"constraints": {"max_array_size": 100}}},
    )
    # backend 256 vs cluster 100 -> 100
    assert _effective_array_cap(_backend(256), "hpc1") == 100
    # backend 50 vs cluster 100 -> 50
    assert _effective_array_cap(_backend(50), "hpc1") == 50


# --------------------------------------------------------------------------- #
# _enforce_array_cap — the repurposed guard (#339 inc 3).
# --------------------------------------------------------------------------- #


def test_guard_silent_over_cap_when_wave_capable() -> None:
    # A wave-capable backend (the default) no longer rejects an over-cap sweep:
    # the wave path splits it into N waves. The guard returns silently.
    _enforce_array_cap(
        _backend(256),
        total_tasks=300,
        backend_name="github-actions",
        cluster=None,
        single_mpi_job=False,
    )


def test_guard_fires_over_cap_for_single_mpi_job() -> None:
    # A single multi-rank MPI job is indivisible — no array to split into waves,
    # so an over-cap MPI submission still fail-louds.
    with pytest.raises(errors.SpecInvalid) as exc:
        _enforce_array_cap(
            _backend(256),
            total_tasks=300,
            backend_name="github-actions",
            cluster=None,
            single_mpi_job=True,
        )
    msg = str(exc.value)
    assert "300" in msg and "256" in msg and "MPI" in msg


def test_guard_fires_over_cap_when_backend_cannot_wave() -> None:
    # A backend that declares can_wave=False cannot split, so the over-cap
    # sweep still fail-louds.
    with pytest.raises(errors.SpecInvalid) as exc:
        _enforce_array_cap(
            _backend(256, can_wave=False),
            total_tasks=300,
            backend_name="oneshot",
            cluster=None,
            single_mpi_job=False,
        )
    msg = str(exc.value)
    assert "300" in msg and "256" in msg and "can_wave" in msg


def test_guard_silent_at_cap() -> None:
    # Exactly at the cap is allowed (one full array).
    _enforce_array_cap(
        _backend(256),
        total_tasks=256,
        backend_name="github-actions",
        cluster=None,
        single_mpi_job=False,
    )


def test_guard_silent_under_cap() -> None:
    _enforce_array_cap(
        _backend(256),
        total_tasks=10,
        backend_name="github-actions",
        cluster=None,
        single_mpi_job=False,
    )


def test_guard_noop_when_uncapped() -> None:
    # SSH family: None cap + no declared cluster limit -> never fires, even huge.
    _enforce_array_cap(
        _backend(None),
        total_tasks=10_000,
        backend_name="slurm",
        cluster=None,
        single_mpi_job=False,
    )


def test_guard_silent_on_cluster_cap_when_wave_capable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "hpc_agent.infra.clusters.load_clusters_config",
        lambda: {"hpc1": {"constraints": {"max_array_size": 100}}},
    )
    # Over the cluster cap but wave-capable: the wave path handles it.
    _enforce_array_cap(
        _backend(None),
        total_tasks=500,
        backend_name="slurm",
        cluster="hpc1",
        single_mpi_job=False,
    )


def test_guard_fires_on_cluster_cap_for_single_mpi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "hpc_agent.infra.clusters.load_clusters_config",
        lambda: {"hpc1": {"constraints": {"max_array_size": 100}}},
    )
    with pytest.raises(errors.SpecInvalid) as exc:
        _enforce_array_cap(
            _backend(None),
            total_tasks=500,
            backend_name="slurm",
            cluster="hpc1",
            single_mpi_job=True,
        )
    assert "100" in str(exc.value) and "hpc1" in str(exc.value)
