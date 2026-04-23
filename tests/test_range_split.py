"""Tests for row-index range-split arithmetic for chunking shims, per submit.md Step 6.

The range computation is performed by the LLM at submission time (see
submit.md Step 6) and baked into manifest commands.  These tests verify
the documented formula so the LLM can be validated against known outputs.
"""

from __future__ import annotations


def _range_split(total_rows: int, chunks: int, ci: int) -> tuple[int, int]:
    """Reference implementation of the range-split formula from submit.md."""
    base = total_rows // chunks
    remainder = total_rows % chunks
    start = base * ci + min(ci, remainder)
    end = start + base + (1 if ci < remainder else 0)
    return start, end


class TestRangeSplit:
    def test_even_division(self):
        ranges = [_range_split(100, 4, i) for i in range(4)]
        assert ranges == [(0, 25), (25, 50), (50, 75), (75, 100)]

    def test_remainder_distributed(self):
        ranges = [_range_split(10, 3, i) for i in range(3)]
        assert ranges == [(0, 4), (4, 7), (7, 10)]
        assert sum(e - s for s, e in ranges) == 10

    def test_single_chunk(self):
        assert _range_split(500, 1, 0) == (0, 500)

    def test_more_chunks_than_rows(self):
        ranges = [_range_split(3, 5, i) for i in range(5)]
        assert sum(e - s for s, e in ranges) == 3
        assert all(e >= s for s, e in ranges)

    def test_no_gaps_or_overlaps(self):
        """Every row is covered exactly once across all chunks."""
        total_rows = 1037
        chunks = 100
        all_indices = []
        for i in range(chunks):
            start, end = _range_split(total_rows, chunks, i)
            all_indices.extend(range(start, end))
        assert sorted(all_indices) == list(range(total_rows))
