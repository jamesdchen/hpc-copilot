"""Tests for ``hpc_agent_pro.forecast.squeue_priority_field`` — fixture-driven."""

from __future__ import annotations

import textwrap

# ruff: noqa: E501 — fixture lines reproduce verbatim cluster output
from hpc_agent_pro.forecast.squeue_priority_field import (
    QueuedJob,
    estimate_rank,
    parse_squeue_priority_field,
)

# ─── parser ────────────────────────────────────────────────────────────


def test_parse_standard_squeue_output() -> None:
    text = textwrap.dedent("""
        JOBID|PRIORITY|PARTITION|USER|STATE|TIME_LEFT
        12345|10000|gpu|alice|PENDING|UNLIMITED
        12346|9500|gpu|bob|RUNNING|1-12:30:00
        12347|9000|cpu|carol|PENDING|N/A
        """).strip()
    parsed = parse_squeue_priority_field(text)
    assert len(parsed) == 3
    assert parsed[0].job_id == "12345"
    assert parsed[0].priority == 10000
    assert parsed[0].user == "alice"
    assert parsed[0].state == "PENDING"
    assert parsed[0].time_left_sec is None
    # 1-12:30:00 = 1 day 12h 30min = 86400 + 45000 + 1800 = wait, recompute.
    # Actually: 1*86400 + 12*3600 + 30*60 + 0 = 86400+43200+1800 = 131400
    assert parsed[1].time_left_sec == 1 * 86400 + 12 * 3600 + 30 * 60


def test_parse_column_order_independent() -> None:
    """Parser keys by column name; reordered headers still work."""
    text = textwrap.dedent("""
        STATE|USER|JOBID|PRIORITY|PARTITION|TIME_LEFT
        PENDING|alice|1|100|gpu|N/A
        """).strip()
    parsed = parse_squeue_priority_field(text)
    assert parsed[0].user == "alice"
    assert parsed[0].job_id == "1"
    assert parsed[0].priority == 100


def test_parse_skips_unparseable_priority() -> None:
    text = textwrap.dedent("""
        JOBID|PRIORITY|PARTITION|USER|STATE|TIME_LEFT
        1|nope|gpu|alice|PENDING|N/A
        2|500|gpu|bob|PENDING|N/A
        """).strip()
    parsed = parse_squeue_priority_field(text)
    assert [j.job_id for j in parsed] == ["2"]


def test_parse_missing_required_column_returns_empty() -> None:
    """Header without PRIORITY → can't parse anything; return [] rather
    than crash mid-row."""
    text = textwrap.dedent("""
        JOBID|PARTITION|USER|STATE
        1|gpu|alice|PENDING
        """).strip()
    assert parse_squeue_priority_field(text) == []


def test_parse_empty_input_returns_empty_list() -> None:
    assert parse_squeue_priority_field("") == []
    assert parse_squeue_priority_field("\n\n") == []


def test_parse_time_left_handles_various_formats() -> None:
    """Pin every documented time-left format — D-HH:MM:SS, HH:MM:SS,
    MM:SS, bare seconds, UNLIMITED, N/A."""
    text = textwrap.dedent("""
        JOBID|PRIORITY|PARTITION|USER|STATE|TIME_LEFT
        a|1|p|u|RUNNING|2-01:00:00
        b|1|p|u|RUNNING|03:30:00
        c|1|p|u|RUNNING|45:00
        d|1|p|u|RUNNING|3600
        e|1|p|u|RUNNING|UNLIMITED
        f|1|p|u|RUNNING|N/A
        """).strip()
    parsed = {j.job_id: j.time_left_sec for j in parse_squeue_priority_field(text)}
    assert parsed["a"] == 2 * 86400 + 3600
    assert parsed["b"] == 3 * 3600 + 30 * 60
    assert parsed["c"] == 45 * 60
    assert parsed["d"] == 3600
    assert parsed["e"] is None
    assert parsed["f"] is None


def test_parse_time_limit_column_when_present() -> None:
    """The TIME_LIMIT column carries the user's requested walltime —
    needed by the drain simulator's backfill mode to decide if a
    pending job fits in a shadow window."""
    text = textwrap.dedent("""
        JOBID|PRIORITY|PARTITION|USER|STATE|TIME_LEFT|TIME_LIMIT
        a|1|gpu|u|PENDING|N/A|01:30:00
        b|1|gpu|u|RUNNING|00:30:00|01:00:00
        c|1|gpu|u|PENDING|N/A|UNLIMITED
    """).strip()
    parsed = {j.job_id: j.time_limit_sec for j in parse_squeue_priority_field(text)}
    assert parsed["a"] == 90 * 60  # 01:30:00
    assert parsed["b"] == 3600
    assert parsed["c"] is None  # UNLIMITED parses to None


def test_time_limit_field_defaults_to_none_when_column_absent() -> None:
    """Older squeue invocations that don't include TIME_LIMIT still
    parse — the field surfaces as None, the simulator falls back to
    its partition default."""
    text = textwrap.dedent("""
        JOBID|PRIORITY|PARTITION|USER|STATE|TIME_LEFT
        a|1|gpu|u|PENDING|N/A
    """).strip()
    parsed = parse_squeue_priority_field(text)
    assert parsed[0].time_limit_sec is None


# ─── rank estimator ────────────────────────────────────────────────────


def _q(job_id: str, priority: int, partition: str = "gpu", state: str = "PENDING") -> QueuedJob:
    return QueuedJob(
        job_id=job_id,
        priority=priority,
        partition=partition,
        user="x",
        state=state,
        time_left_sec=None,
    )


def test_estimate_rank_counts_only_higher_priority_pendings() -> None:
    queue = [_q("1", 100), _q("2", 200), _q("3", 50)]
    out = estimate_rank(queue, new_priority=150)
    # Only job 2 (priority 200) is ahead.
    assert out.rank_overall == 2
    assert out.pending_ahead_overall == 1


def test_estimate_rank_ignores_running_jobs() -> None:
    """Running jobs aren't competitors for the front of the pending
    queue. Only PENDING counts."""
    queue = [_q("1", 999, state="RUNNING"), _q("2", 100, state="PENDING")]
    out = estimate_rank(queue, new_priority=50)
    assert out.rank_overall == 2  # only job 2 is ahead, RUNNING ignored


def test_estimate_rank_partition_scoped_count() -> None:
    """When a partition is given, only competitors in that partition
    count toward in-partition rank."""
    queue = [
        _q("1", 200, partition="gpu"),
        _q("2", 200, partition="cpu"),
        _q("3", 100, partition="gpu"),
    ]
    out = estimate_rank(queue, new_priority=150, partition="gpu")
    assert out.pending_ahead_overall == 2  # both 200-priority jobs
    assert out.pending_ahead_in_partition == 1  # only the gpu one
    assert out.rank_in_partition == 2


def test_estimate_rank_top_of_queue() -> None:
    queue = [_q("1", 100), _q("2", 50)]
    out = estimate_rank(queue, new_priority=999)
    assert out.rank_overall == 1
    assert out.pending_ahead_overall == 0


def test_estimate_rank_empty_queue() -> None:
    out = estimate_rank([], new_priority=100)
    assert out.rank_overall == 1
    assert out.pending_ahead_overall == 0
