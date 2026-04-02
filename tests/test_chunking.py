"""Tests for hpc.chunking protocol."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from hpc.chunking import ChunkContext, chunk_context, collect_chunks


class TestChunkContext:
    def test_local_defaults(self, monkeypatch):
        monkeypatch.delenv("CHUNK_ID", raising=False)
        monkeypatch.delenv("TOTAL_CHUNKS", raising=False)
        monkeypatch.delenv("RESULT_DIR", raising=False)
        ctx = chunk_context()
        assert ctx.chunk_id == 0
        assert ctx.total_chunks == 1
        assert ctx.result_dir == Path(".")

    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("CHUNK_ID", "3")
        monkeypatch.setenv("TOTAL_CHUNKS", "10")
        monkeypatch.setenv("RESULT_DIR", "/tmp/results")
        ctx = chunk_context()
        assert ctx.chunk_id == 3
        assert ctx.total_chunks == 10
        assert ctx.result_dir == Path("/tmp/results")

    def test_split_single_chunk_returns_full_range(self):
        ctx = ChunkContext(chunk_id=0, total_chunks=1, result_dir=Path("."))
        assert ctx.split(range(100)) == range(100)

    def test_split_int_shorthand(self):
        ctx = ChunkContext(chunk_id=0, total_chunks=1, result_dir=Path("."))
        assert ctx.split(100) == range(100)

    def test_split_even_division(self):
        """4 chunks of 100 items → 25 each."""
        ranges = []
        for i in range(4):
            ctx = ChunkContext(chunk_id=i, total_chunks=4, result_dir=Path("."))
            ranges.append(ctx.split(100))
        assert ranges[0] == range(0, 25)
        assert ranges[1] == range(25, 50)
        assert ranges[2] == range(50, 75)
        assert ranges[3] == range(75, 100)

    def test_split_with_remainder(self):
        """3 chunks of 10 items → sizes 4, 3, 3."""
        ranges = []
        for i in range(3):
            ctx = ChunkContext(chunk_id=i, total_chunks=3, result_dir=Path("."))
            ranges.append(ctx.split(10))
        assert len(ranges[0]) == 4  # gets extra from remainder
        assert len(ranges[1]) == 3
        assert len(ranges[2]) == 3
        # Non-overlapping and complete
        all_indices = []
        for r in ranges:
            all_indices.extend(r)
        assert sorted(all_indices) == list(range(10))

    def test_split_preserves_offset(self):
        """range(50, 150) should keep start=50."""
        ctx = ChunkContext(chunk_id=0, total_chunks=2, result_dir=Path("."))
        r = ctx.split(range(50, 150))
        assert r.start == 50
        assert r.stop == 100
        ctx2 = ChunkContext(chunk_id=1, total_chunks=2, result_dir=Path("."))
        r2 = ctx2.split(range(50, 150))
        assert r2.start == 100
        assert r2.stop == 150

    def test_split_covers_all_indices_with_offset(self):
        """All items accounted for with offset range."""
        items = range(200, 513)
        all_indices = []
        for i in range(7):
            ctx = ChunkContext(chunk_id=i, total_chunks=7, result_dir=Path("."))
            all_indices.extend(ctx.split(items))
        assert sorted(all_indices) == list(items)

    def test_output_path_default(self):
        ctx = ChunkContext(chunk_id=4, total_chunks=10, result_dir=Path("/results/run1"))
        assert ctx.output_path() == Path("/results/run1/results_chunk_5.csv")

    def test_output_path_custom_prefix(self):
        ctx = ChunkContext(chunk_id=0, total_chunks=1, result_dir=Path("."))
        assert ctx.output_path("scaling_result") == Path("scaling_result_1.csv")

    def test_frozen(self):
        ctx = ChunkContext(chunk_id=0, total_chunks=1, result_dir=Path("."))
        with pytest.raises(AttributeError):
            ctx.chunk_id = 5  # type: ignore[misc]


class TestCollectChunks:
    def _write_csv(self, path, n=5, start_date="2020-01-01", include_date=True):
        """Helper to write a chunk CSV."""
        data = {"val": list(range(n))}
        if include_date:
            data["date"] = pd.date_range(start_date, periods=n, freq="h").tolist()
        pd.DataFrame(data).to_csv(path, index=False)

    def test_empty_directory(self, tmp_path):
        df = collect_chunks(tmp_path)
        assert df.empty

    def test_single_csv(self, tmp_path):
        self._write_csv(tmp_path / "results_chunk_1.csv", n=5)
        df = collect_chunks(tmp_path)
        assert len(df) == 5
        assert isinstance(df.index, pd.DatetimeIndex)

    def test_multiple_csvs_concatenated_and_sorted(self, tmp_path):
        self._write_csv(tmp_path / "results_chunk_1.csv", n=3, start_date="2020-01-02")
        self._write_csv(tmp_path / "results_chunk_2.csv", n=3, start_date="2020-01-01")
        df = collect_chunks(tmp_path)
        assert len(df) == 6
        # Should be sorted by date — chunk_2 dates come first
        assert df.index[0] < df.index[3]

    def test_no_date_column(self, tmp_path):
        self._write_csv(tmp_path / "results_chunk_1.csv", n=4, include_date=False)
        df = collect_chunks(tmp_path)
        assert len(df) == 4
        assert not isinstance(df.index, pd.DatetimeIndex)

    def test_unreadable_file_skipped(self, tmp_path):
        self._write_csv(tmp_path / "results_chunk_1.csv", n=3)
        (tmp_path / "results_chunk_2.csv").write_bytes(b"\x80\x81\x82\x83")
        self._write_csv(tmp_path / "results_chunk_3.csv", n=4)
        df = collect_chunks(tmp_path)
        assert len(df) == 7  # 3 + 4, skipping the bad file

    def test_custom_pattern(self, tmp_path):
        self._write_csv(tmp_path / "results_chunk_1.csv", n=5)
        self._write_csv(tmp_path / "output_1.csv", n=3)
        df = collect_chunks(tmp_path, pattern="output_*.csv")
        assert len(df) == 3


class TestCounters:
    def test_counter_path(self):
        ctx = ChunkContext(chunk_id=4, total_chunks=10, result_dir=Path("/results"))
        assert ctx.counter_path() == Path("/results/_counters_5.json")

    def test_update_counters_writes_json(self, tmp_path):
        ctx = ChunkContext(chunk_id=0, total_chunks=1, result_dir=tmp_path)
        ctx.update_counters(rows_processed=5000, errors=2)
        import json

        data = json.loads(ctx.counter_path().read_text())
        assert data["rows_processed"] == 5000
        assert data["errors"] == 2

    def test_read_counters(self, tmp_path):
        ctx = ChunkContext(chunk_id=0, total_chunks=1, result_dir=tmp_path)
        ctx.update_counters(rows_processed=42, total_rows=100)
        result = ctx.read_counters()
        assert result == {"rows_processed": 42, "total_rows": 100}

    def test_read_counters_missing_file(self, tmp_path):
        ctx = ChunkContext(chunk_id=0, total_chunks=1, result_dir=tmp_path)
        assert ctx.read_counters() == {}

    def test_update_counters_overwrites(self, tmp_path):
        ctx = ChunkContext(chunk_id=0, total_chunks=1, result_dir=tmp_path)
        ctx.update_counters(rows_processed=10, errors=1)
        ctx.update_counters(rows_processed=50, errors=3)
        result = ctx.read_counters()
        assert result == {"rows_processed": 50, "errors": 3}

    def test_update_counters_atomic(self, tmp_path):
        ctx = ChunkContext(chunk_id=0, total_chunks=1, result_dir=tmp_path)
        ctx.update_counters(rows_processed=100)
        tmp_file = ctx.counter_path().with_suffix(".tmp")
        assert not tmp_file.exists()
        assert ctx.counter_path().exists()
