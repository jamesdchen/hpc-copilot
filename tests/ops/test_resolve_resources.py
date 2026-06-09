"""Tests for the ``resolve-resources`` composite primitive (WS5 #5).

Pins hpc-submit Step 6's three resolutions and their provenance:

* ``walltime_sec`` — caller override, runtime-prior p95 × safety_mult,
  and every cold-start path (no profile, verb absent/erroring,
  needs_canary, no matching quantile row). A missing read-runtime-prior
  verb is cold-start, NOT an error — this is the critical contract.
* ``gpu_type`` — caller override vs. clusters.<cluster>.gpu_types[0] vs.
  a cluster that declares none.
* ``partition`` — caller override, delegation to recommend-partition,
  and the no-config null path.

The ``read-runtime-prior`` subprocess is mocked at
:func:`_read_runtime_prior_p95`'s ``subprocess.run`` seam, and the
clusters.yaml read at :func:`load_clusters_config`, so these tests don't
depend on a real binary or on-disk config.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from hpc_agent.ops import resolve_resources as rr


def _prior_envelope(quantiles: dict[str, Any], *, needs_canary: bool = False) -> str:
    """A read-runtime-prior stdout envelope carrying *quantiles*."""
    return json.dumps(
        {
            "ok": True,
            "idempotent": True,
            "data": {
                "profile": "train",
                "cluster": "hoffman2",
                "now_iso": "2026-06-04T00:00:00Z",
                "needs_canary": needs_canary,
                "quantiles": quantiles,
                "total_samples": 0 if needs_canary else 7,
            },
        }
    )


class _FakeProc:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def _patch_prior(
    monkeypatch: pytest.MonkeyPatch, stdout: str | None, *, raises: Exception | None = None
) -> None:
    """Patch the read-runtime-prior subprocess to return *stdout* (or raise)."""

    def fake_run(*_a: Any, **_k: Any) -> _FakeProc:
        if raises is not None:
            raise raises
        return _FakeProc(stdout if stdout is not None else "")

    monkeypatch.setattr(rr.subprocess, "run", fake_run)


def _patch_clusters(monkeypatch: pytest.MonkeyPatch, config: dict[str, Any]) -> None:
    """Patch load_clusters_config to return *config* regardless of path."""
    monkeypatch.setattr(rr, "load_clusters_config", lambda path=None: config)


# ── gpu_type resolution ──────────────────────────────────────────────────────


class TestGpuType:
    def test_caller_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_clusters(monkeypatch, {"hoffman2": {"gpu_types": ["v100"]}})
        out = rr.resolve_resources(cluster="hoffman2", gpu_type="a100")
        assert out["gpu_type"] == "a100"
        assert out["provenance"]["gpu_type"] == "caller"

    def test_cluster_default_is_first_gpu(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_clusters(monkeypatch, {"hoffman2": {"gpu_types": ["a100", "h200"]}})
        out = rr.resolve_resources(cluster="hoffman2")
        assert out["gpu_type"] == "a100"
        assert out["provenance"]["gpu_type"] == "cluster_default"

    def test_cluster_declares_none_yields_null(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_clusters(monkeypatch, {"hoffman2": {"gpu_types": []}})
        out = rr.resolve_resources(cluster="hoffman2")
        assert out["gpu_type"] is None
        assert out["provenance"]["gpu_type"] == "cluster_declares_none"

    def test_unknown_cluster_yields_null(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_clusters(monkeypatch, {})
        out = rr.resolve_resources(cluster="ghost")
        assert out["gpu_type"] is None
        assert out["provenance"]["gpu_type"] == "cluster_declares_none"


# ── walltime resolution ──────────────────────────────────────────────────────


class TestWalltimeCaller:
    def test_caller_override_skips_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_clusters(monkeypatch, {"hoffman2": {"gpu_types": ["a100"]}})

        # If the probe ran it would blow up; assert it does NOT.
        def boom(*_a: Any, **_k: Any) -> None:
            raise AssertionError("read-runtime-prior must not run when caller supplies walltime")

        monkeypatch.setattr(rr.subprocess, "run", boom)
        out = rr.resolve_resources(cluster="hoffman2", profile="train", walltime_sec=7200)
        assert out["walltime_sec"] == 7200
        assert out["provenance"]["walltime_sec"] == "caller"


class TestWalltimePrior:
    def test_prior_p95_times_safety_mult(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_clusters(monkeypatch, {"hoffman2": {"gpu_types": ["a100"]}})
        _patch_prior(monkeypatch, _prior_envelope({"a100": {"p50": 1000, "p95": 2000}}))
        out = rr.resolve_resources(cluster="hoffman2", profile="train")
        # 2000 * 1.30 = 2600
        assert out["walltime_sec"] == 2600
        assert out["provenance"]["walltime_sec"] == "prior_p95"

    def test_custom_safety_mult(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_clusters(monkeypatch, {"hoffman2": {"gpu_types": ["a100"]}})
        _patch_prior(monkeypatch, _prior_envelope({"a100": {"p50": 100, "p95": 1000}}))
        out = rr.resolve_resources(cluster="hoffman2", profile="train", safety_mult=1.5)
        assert out["walltime_sec"] == 1500

    def test_selects_matching_gpu_quantile_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # gpu_type resolves to a100 (cluster default); the a100 row must be picked.
        _patch_clusters(monkeypatch, {"hoffman2": {"gpu_types": ["a100"]}})
        _patch_prior(
            monkeypatch,
            _prior_envelope({"v100": {"p50": 10, "p95": 99}, "a100": {"p50": 100, "p95": 1000}}),
        )
        out = rr.resolve_resources(cluster="hoffman2", profile="train")
        assert out["walltime_sec"] == 1300  # 1000 * 1.30, the a100 row


class TestWalltimeColdStart:
    def test_no_profile_is_cold_start(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_clusters(monkeypatch, {"hoffman2": {"gpu_types": ["a100"]}})
        out = rr.resolve_resources(cluster="hoffman2")
        assert out["walltime_sec"] is None
        assert out["provenance"]["walltime_sec"] == "cold_start_no_profile"

    def test_verb_absent_is_cold_start_not_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Core install: argparse "invalid choice" exits 2 with non-JSON stdout.
        _patch_clusters(monkeypatch, {"hoffman2": {"gpu_types": ["a100"]}})
        _patch_prior(monkeypatch, "usage: hpc-agent ...\nerror: invalid choice")
        out = rr.resolve_resources(cluster="hoffman2", profile="train")
        assert out["walltime_sec"] is None
        assert out["provenance"]["walltime_sec"] == "cold_start_prior_verb_unavailable"

    def test_spawn_oserror_is_cold_start(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_clusters(monkeypatch, {"hoffman2": {"gpu_types": ["a100"]}})
        _patch_prior(monkeypatch, None, raises=OSError("no such binary"))
        out = rr.resolve_resources(cluster="hoffman2", profile="train")
        assert out["walltime_sec"] is None
        assert out["provenance"]["walltime_sec"] == "cold_start_prior_verb_unavailable"

    def test_timeout_is_cold_start(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_clusters(monkeypatch, {"hoffman2": {"gpu_types": ["a100"]}})
        _patch_prior(monkeypatch, None, raises=subprocess.TimeoutExpired(cmd="x", timeout=30.0))
        out = rr.resolve_resources(cluster="hoffman2", profile="train")
        assert out["walltime_sec"] is None
        assert out["provenance"]["walltime_sec"] == "cold_start_prior_verb_unavailable"

    def test_not_ok_envelope_is_cold_start(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_clusters(monkeypatch, {"hoffman2": {"gpu_types": ["a100"]}})
        _patch_prior(monkeypatch, json.dumps({"ok": False, "error_code": "internal"}))
        out = rr.resolve_resources(cluster="hoffman2", profile="train")
        assert out["walltime_sec"] is None
        assert out["provenance"]["walltime_sec"] == "cold_start_prior_verb_unavailable"

    def test_needs_canary_is_cold_start_no_samples(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_clusters(monkeypatch, {"hoffman2": {"gpu_types": ["a100"]}})
        _patch_prior(monkeypatch, _prior_envelope({}, needs_canary=True))
        out = rr.resolve_resources(cluster="hoffman2", profile="train")
        assert out["walltime_sec"] is None
        assert out["provenance"]["walltime_sec"] == "cold_start_no_samples"

    def test_empty_quantiles_cold_start_no_samples(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_clusters(monkeypatch, {"hoffman2": {"gpu_types": ["a100"]}})
        _patch_prior(monkeypatch, _prior_envelope({}))
        out = rr.resolve_resources(cluster="hoffman2", profile="train")
        assert out["walltime_sec"] is None
        assert out["provenance"]["walltime_sec"] == "cold_start_no_samples"

    def test_no_matching_gpu_row_falls_to_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # gpu_type resolves to a100 but the prior only has a v100 row — take
        # the first available row (any prior beats none).
        _patch_clusters(monkeypatch, {"hoffman2": {"gpu_types": ["a100"]}})
        _patch_prior(monkeypatch, _prior_envelope({"v100": {"p50": 10, "p95": 500}}))
        out = rr.resolve_resources(cluster="hoffman2", profile="train")
        assert out["walltime_sec"] == 650  # 500 * 1.30
        assert out["provenance"]["walltime_sec"] == "prior_p95"


# ── partition resolution (REUSE recommend-partition) ─────────────────────────


class TestPartition:
    def test_caller_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_clusters(monkeypatch, {"hoffman2": {"gpu_types": ["a100"]}})
        out = rr.resolve_resources(cluster="hoffman2", partition="gpu")
        assert out["partition"] == "gpu"
        assert out["provenance"]["partition"] == "caller"

    def test_no_partitions_yields_null(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_clusters(monkeypatch, {"hoffman2": {"gpu_types": ["a100"]}})
        out = rr.resolve_resources(cluster="hoffman2")
        assert out["partition"] is None
        assert out["provenance"]["partition"] == "no_partitions_supplied"

    def test_delegates_to_recommend_partition_short_walltime(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A short caller walltime + a debug partition → recommend-partition
        # routes to debug. We assert resolve-resources REUSES that verdict
        # rather than reimplementing the routing.
        _patch_clusters(monkeypatch, {"hoffman2": {"gpu_types": ["a100"]}})
        out = rr.resolve_resources(
            cluster="hoffman2",
            walltime_sec=600,
            partitions=[
                {"name": "debug", "priority_tier": 10, "walltime_cap_sec": 3600, "is_debug": True},
                {"name": "normal", "priority_tier": 1},
            ],
        )
        assert out["partition"] == "debug"
        assert out["provenance"]["partition"] == "recommend_partition:debug_short_walltime"

    def test_delegates_overrun_refusal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A long walltime > debug cap → recommend-partition refuses debug and
        # routes to the non-debug fallback.
        _patch_clusters(monkeypatch, {"hoffman2": {"gpu_types": ["a100"]}})
        out = rr.resolve_resources(
            cluster="hoffman2",
            walltime_sec=99999,
            partitions=[
                {"name": "debug", "priority_tier": 10, "walltime_cap_sec": 3600, "is_debug": True},
                {"name": "normal", "priority_tier": 1},
            ],
        )
        assert out["partition"] == "normal"
        assert out["provenance"]["partition"] == "recommend_partition:debug_overrun_refused"

    def test_user_preferred_partition_forwarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_clusters(monkeypatch, {"hoffman2": {"gpu_types": ["a100"]}})
        out = rr.resolve_resources(
            cluster="hoffman2",
            walltime_sec=600,
            user_preferred_partition="normal",
            partitions=[
                {"name": "debug", "priority_tier": 10, "walltime_cap_sec": 3600, "is_debug": True},
                {"name": "normal", "priority_tier": 1},
            ],
        )
        assert out["partition"] == "normal"
        assert out["provenance"]["partition"] == "recommend_partition:user_preference_honoured"

    def test_cold_start_partition_uses_1h_probe_walltime(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # walltime cold-start (null) → partition routing still works via the
        # 1h probe walltime, landing a short job on debug.
        _patch_clusters(monkeypatch, {"hoffman2": {"gpu_types": ["a100"]}})
        out = rr.resolve_resources(
            cluster="hoffman2",
            partitions=[
                {"name": "debug", "priority_tier": 10, "walltime_cap_sec": 3600, "is_debug": True},
                {"name": "normal", "priority_tier": 1},
            ],
        )
        assert out["walltime_sec"] is None
        assert out["partition"] == "debug"


# ── full-shape contract ──────────────────────────────────────────────────────


class TestOutputShape:
    def test_all_keys_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_clusters(monkeypatch, {"hoffman2": {"gpu_types": ["a100"]}})
        out = rr.resolve_resources(cluster="hoffman2")
        assert set(out) == {
            "walltime_sec",
            "gpu_type",
            "partition",
            "mpi_pe",
            "provenance",
            "elapsed_total_sec",
        }
        assert set(out["provenance"]) == {"walltime_sec", "gpu_type", "partition", "mpi_pe"}
        assert out["elapsed_total_sec"] >= 0
        # Non-MPI call (no mpi_ranks): mpi_pe is null with the not_mpi provenance.
        assert out["mpi_pe"] is None
        assert out["provenance"]["mpi_pe"] == "not_mpi"


# ── mpi_pe resolution (#293) ─────────────────────────────────────────────────


class TestMpiPe:
    """The MPI-aware path: caller override, auto-derivation from the cluster's
    parallel_environments, and the null cases (not an MPI submit / no enum)."""

    _PES = [
        {"name": "smp", "source": "pe", "kind": "smp", "raw": {"slots": 16}},
        {"name": "mpi", "source": "pe", "kind": "mpi", "raw": {"slots": 256}},
        {"name": "mpi_small", "source": "pe", "kind": "mpi", "raw": {"slots": 32}},
    ]

    def test_not_mpi_when_no_ranks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_clusters(monkeypatch, {"c": {}})
        out = rr.resolve_resources(cluster="c", parallel_environments=self._PES)
        assert out["mpi_pe"] is None
        assert out["provenance"]["mpi_pe"] == "not_mpi"

    def test_auto_derives_tightest_pe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_clusters(monkeypatch, {"c": {}})
        out = rr.resolve_resources(cluster="c", mpi_ranks=16, parallel_environments=self._PES)
        assert out["mpi_pe"] == "mpi_small"
        assert out["provenance"]["mpi_pe"].startswith("recommend_pe:tightest_fit")

    def test_caller_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_clusters(monkeypatch, {"c": {}})
        out = rr.resolve_resources(
            cluster="c", mpi_pe="orte", mpi_ranks=16, parallel_environments=self._PES
        )
        assert out["mpi_pe"] == "orte"
        assert out["provenance"]["mpi_pe"] == "caller"

    def test_no_enumeration_supplied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_clusters(monkeypatch, {"c": {}})
        out = rr.resolve_resources(cluster="c", mpi_ranks=16)
        assert out["mpi_pe"] is None
        assert out["provenance"]["mpi_pe"] == "no_parallel_environments_supplied"

    def test_ranks_exceed_all_pes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_clusters(monkeypatch, {"c": {}})
        out = rr.resolve_resources(cluster="c", mpi_ranks=10000, parallel_environments=self._PES)
        assert out["mpi_pe"] is None
        assert "no_pe_fits_ranks" in out["provenance"]["mpi_pe"]
