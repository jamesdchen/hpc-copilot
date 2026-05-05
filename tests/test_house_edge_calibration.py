"""Tests for the test-only ETA → empirical wait calibration loop.

Covers the three new atoms wiring SLURM ``--test-only`` against
observed reality:

* :func:`compute_house_edge_by_gpu_type` — bucketed calibration ratios.
* :func:`calibrate_probes` — apply ratios to lattice probe ETAs.
* :func:`pick_earliest_calibrated` — rank by adjusted ETA.
* Planner integration — calibrated fields appear on probe report items.
"""

from __future__ import annotations

from claude_hpc.orchestrator.backfill import (
    CALIBRATION_FACTOR_CEILING,
    CALIBRATION_FACTOR_FLOOR,
    BackfillProbe,
    ResourceTuple,
    calibrate_probes,
    pick_earliest_calibrated,
)
from claude_hpc.orchestrator.calibration import (
    HouseEdge,
    compute_house_edge_by_gpu_type,
)


def _sample(gpu_type: str, predicted: int, actual_offset: int) -> dict:
    """Sample with predicted_eta_sec + matched (submitted, started) ISO pair.

    *actual_offset* is the queue wait we'll bake in: started = submitted + offset.
    """
    submitted = "2026-04-01T00:00:00+00:00"
    # Build an ISO timestamp `actual_offset` seconds after submitted.
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc)
    started = (base + timedelta(seconds=actual_offset)).isoformat()
    return {
        "gpu_type": gpu_type,
        "predicted_eta_sec": predicted,
        "submitted_at_iso": submitted,
        "started_at": started,
    }


class TestComputeHouseEdgeByGpuType:
    def test_buckets_by_gpu_type(self):
        samples = [_sample("a100", predicted=300, actual_offset=900) for _ in range(5)] + [
            _sample("a40", predicted=300, actual_offset=150) for _ in range(5)
        ]
        edges = compute_house_edge_by_gpu_type(samples)
        assert set(edges.keys()) == {"a100", "a40"}
        # a100: actual/predicted = 900/300 = 3.0 (scheduler optimistic)
        assert abs(edges["a100"].calibration_ratio - 3.0) < 0.01
        # a40: 150/300 = 0.5 (scheduler pessimistic)
        assert abs(edges["a40"].calibration_ratio - 0.5) < 0.01

    def test_drops_undersized_buckets(self):
        # min_samples=5 (default); a40 has only 3 paired samples.
        samples = [_sample("a100", predicted=300, actual_offset=900) for _ in range(5)] + [
            _sample("a40", predicted=300, actual_offset=150) for _ in range(3)
        ]
        edges = compute_house_edge_by_gpu_type(samples)
        assert "a100" in edges
        assert "a40" not in edges

    def test_skips_samples_without_gpu_type(self):
        samples = (
            [_sample("a100", predicted=300, actual_offset=600) for _ in range(5)]
            + [{"predicted_eta_sec": 300} for _ in range(3)]  # no gpu_type
        )
        edges = compute_house_edge_by_gpu_type(samples)
        assert set(edges.keys()) == {"a100"}

    def test_empty_samples_returns_empty(self):
        assert compute_house_edge_by_gpu_type([]) == {}

    def test_min_samples_threshold_is_configurable(self):
        samples = [_sample("a100", predicted=300, actual_offset=600) for _ in range(3)]
        # Default 5: dropped.
        assert compute_house_edge_by_gpu_type(samples) == {}
        # Lower threshold: included.
        edges = compute_house_edge_by_gpu_type(samples, min_samples=2)
        assert "a100" in edges


class TestCalibrateProbes:
    @staticmethod
    def _probe(constraint: str, eta: int | None) -> BackfillProbe:
        return BackfillProbe(
            tuple_=ResourceTuple(constraint=constraint, walltime_sec=3600, mem_mb=8000, cpus=2),
            eta_sec=eta,
            raw_test_only="",
        )

    def test_applies_factor_to_eta(self):
        probes = [self._probe("a100", 300)]
        edges = {"a100": HouseEdge(10, 600.0, 600.0, 600.0, 3.0)}
        calibrated = calibrate_probes(
            probes,
            edges_by_gpu_type=edges,
            gpu_types_for_constraint=lambda c: [c],
        )
        assert calibrated[0].eta_sec_calibrated == 900  # 300 * 3.0
        assert calibrated[0].factor == 3.0

    def test_no_calibration_data_passes_eta_through(self):
        probes = [self._probe("a100", 300)]
        calibrated = calibrate_probes(
            probes,
            edges_by_gpu_type={},
            gpu_types_for_constraint=lambda c: [c],
        )
        assert calibrated[0].eta_sec_calibrated == 300
        assert calibrated[0].factor is None

    def test_alternation_uses_worst_case_factor(self):
        probes = [self._probe("a40|a100", 300)]
        edges = {
            "a40": HouseEdge(10, 100.0, 100.0, 100.0, 0.5),
            "a100": HouseEdge(10, 600.0, 600.0, 600.0, 3.0),
        }
        calibrated = calibrate_probes(
            probes,
            edges_by_gpu_type=edges,
            gpu_types_for_constraint=lambda c: c.split("|"),
        )
        # Worst-case = max ratio = 3.0.
        assert calibrated[0].factor == 3.0
        assert calibrated[0].eta_sec_calibrated == 900

    def test_clamps_to_ceiling(self):
        probes = [self._probe("a100", 100)]
        edges = {"a100": HouseEdge(10, 999.0, 999.0, 999.0, 50.0)}  # absurd
        calibrated = calibrate_probes(
            probes,
            edges_by_gpu_type=edges,
            gpu_types_for_constraint=lambda c: [c],
        )
        assert calibrated[0].factor == CALIBRATION_FACTOR_CEILING
        assert calibrated[0].eta_sec_calibrated == int(100 * CALIBRATION_FACTOR_CEILING)

    def test_clamps_to_floor(self):
        probes = [self._probe("a100", 1000)]
        edges = {"a100": HouseEdge(10, -999.0, -999.0, -999.0, 0.001)}  # absurd
        calibrated = calibrate_probes(
            probes,
            edges_by_gpu_type=edges,
            gpu_types_for_constraint=lambda c: [c],
        )
        assert calibrated[0].factor == CALIBRATION_FACTOR_FLOOR
        assert calibrated[0].eta_sec_calibrated == int(1000 * CALIBRATION_FACTOR_FLOOR)

    def test_none_eta_passes_through_as_none(self):
        probes = [self._probe("a100", None)]
        edges = {"a100": HouseEdge(10, 600.0, 600.0, 600.0, 3.0)}
        calibrated = calibrate_probes(
            probes,
            edges_by_gpu_type=edges,
            gpu_types_for_constraint=lambda c: [c],
        )
        assert calibrated[0].eta_sec_calibrated is None
        assert calibrated[0].factor is None


class TestPickEarliestCalibrated:
    @staticmethod
    def _probe(constraint: str, walltime: int, eta: int | None) -> BackfillProbe:
        return BackfillProbe(
            tuple_=ResourceTuple(constraint=constraint, walltime_sec=walltime, mem_mb=8000, cpus=2),
            eta_sec=eta,
            raw_test_only="",
        )

    def test_calibration_flips_winner(self):
        """The whole point: raw rank ≠ calibrated rank when the predictor is biased."""
        probes = [
            self._probe("a100", 7200, 100),  # raw winner; flagship pool, optimistic
            self._probe("a40", 7200, 200),  # raw loser; cheap pool, pessimistic
        ]
        edges = {
            "a100": HouseEdge(10, 900.0, 900.0, 900.0, 5.0),  # 5× too optimistic
            "a40": HouseEdge(10, -100.0, -100.0, -100.0, 0.5),  # 2× too pessimistic
        }
        calibrated = calibrate_probes(
            probes,
            edges_by_gpu_type=edges,
            gpu_types_for_constraint=lambda c: [c],
        )
        # Calibrated ETAs: a100 = 500, a40 = 100. a40 should win.
        winner = pick_earliest_calibrated(calibrated)
        assert winner is not None
        assert winner.probe.tuple_.constraint == "a40"

    def test_empty_returns_none(self):
        assert pick_earliest_calibrated([]) is None

    def test_all_none_etas_returns_none(self):
        probes = [self._probe("a100", 7200, None), self._probe("a40", 7200, None)]
        calibrated = calibrate_probes(
            probes,
            edges_by_gpu_type={},
            gpu_types_for_constraint=lambda c: [c],
        )
        assert pick_earliest_calibrated(calibrated) is None

    def test_tie_breaks_by_smaller_walltime(self):
        probes = [
            self._probe("a100", 14400, 300),
            self._probe("a100", 7200, 300),
        ]
        calibrated = calibrate_probes(
            probes,
            edges_by_gpu_type={},
            gpu_types_for_constraint=lambda c: [c],
        )
        winner = pick_earliest_calibrated(calibrated)
        assert winner is not None
        assert winner.probe.tuple_.walltime_sec == 7200
