"""Tests for reduce_metrics in hpc_mapreduce.reduce.metrics."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from hpc_mapreduce.reduce.metrics import reduce_metrics

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_metrics(tmp_path: Path, chunk_id: int, **metrics: float) -> None:
    path = tmp_path / f"metrics_chunk_{chunk_id + 1}.json"
    path.write_text(json.dumps(metrics))


# ---------------------------------------------------------------------------
# TestReduceMetrics
# ---------------------------------------------------------------------------


class TestReduceMetrics:
    def test_weighted_mean(self, tmp_path: Path) -> None:
        """3 chunks with different n_samples — weighted average of loss."""
        # chunk 1: n=100, loss=1.0
        # chunk 2: n=200, loss=2.0
        # chunk 3: n=300, loss=3.0
        # weighted mean = (100*1 + 200*2 + 300*3) / 600 = 1400/600
        for i, (n, loss) in enumerate([(100, 1.0), (200, 2.0), (300, 3.0)]):
            _write_metrics(tmp_path, i, n_samples=n, loss=loss)

        result = reduce_metrics(tmp_path, total_chunks=3)
        expected_loss = (100 * 1.0 + 200 * 2.0 + 300 * 3.0) / 600
        assert result["loss"] == pytest.approx(expected_loss)

    def test_n_samples_summed(self, tmp_path: Path) -> None:
        for i, n in enumerate([100, 200, 300]):
            _write_metrics(tmp_path, i, n_samples=n)

        result = reduce_metrics(tmp_path, total_chunks=3)
        assert result["n_samples"] == 600

    def test_missing_chunks_skipped(self, tmp_path: Path) -> None:
        """Only 2 of 3 chunks present — still aggregates correctly."""
        _write_metrics(tmp_path, 0, n_samples=50, loss=1.0)
        _write_metrics(tmp_path, 2, n_samples=50, loss=3.0)

        result = reduce_metrics(tmp_path, total_chunks=3)
        assert result["n_samples"] == 100
        assert result["loss"] == pytest.approx(2.0)  # equal weights → simple mean

    def test_no_n_samples_equal_weight(self, tmp_path: Path) -> None:
        """Without n_samples, all chunks get weight=1 → simple mean."""
        for i, loss in enumerate([2.0, 4.0]):
            _write_metrics(tmp_path, i, loss=loss)

        result = reduce_metrics(tmp_path, total_chunks=2)
        assert result["loss"] == pytest.approx(3.0)
        assert "n_samples" not in result

    def test_empty_directory(self, tmp_path: Path) -> None:
        assert reduce_metrics(tmp_path, total_chunks=3) == {}

    def test_corrupt_json_skipped(self, tmp_path: Path) -> None:
        _write_metrics(tmp_path, 0, loss=1.0)
        # Write corrupt chunk 2
        (tmp_path / "metrics_chunk_2.json").write_text("{bad json!!!}")

        result = reduce_metrics(tmp_path, total_chunks=2)
        assert result["loss"] == pytest.approx(1.0)
