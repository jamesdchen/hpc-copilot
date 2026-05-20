"""Tests for ``hpc_agent.infra.slurm_reservations``.

Pure parsers, fixture-driven (real ``scontrol show res`` and
``sacctmgr show qos`` output samples). Hypothesis is the wrong tool
here — the input space is "what SLURM emits" not "any string."
"""

from __future__ import annotations

import textwrap

# ruff: noqa: E501 — fixture lines reproduce verbatim cluster output
from hpc_agent.infra.slurm_reservations import (
    QosLimit,
    ReservationHold,
    held_node_set,
    parse_sacctmgr_qos,
    parse_slurm_reservations,
    reservations_active_at,
)

# ─── reservation parser ────────────────────────────────────────────────


def test_parse_reservation_extracts_basic_fields() -> None:
    """Standard ``scontrol show res`` output: ReservationName, Nodes,
    StartTime, EndTime, Users land in the right fields."""
    text = textwrap.dedent("""
        ReservationName=maintenance StartTime=2026-04-15T03:00:00 EndTime=2026-04-15T05:00:00 Duration=02:00:00
           Nodes=cn001,cn002,cn003 NodeCnt=3 CoreCnt=96 Features=(null) PartitionName=normal Flags=MAINT,IGNORE_JOBS
           TRES=cpu=96 Users=root Accounts=(null) Licenses=(null) State=ACTIVE BurstBuffer=(null) Watts=n/a
           MaxStartDelay=(null)
        """).strip()
    parsed = parse_slurm_reservations(text)
    assert len(parsed) == 1
    r = parsed[0]
    assert r.name == "maintenance"
    assert r.nodes == ("cn001", "cn002", "cn003")
    assert r.start_iso == "2026-04-15T03:00:00+00:00"
    assert r.end_iso == "2026-04-15T05:00:00+00:00"
    assert r.users == ("root",)
    assert r.flags == ("MAINT", "IGNORE_JOBS")


def test_parse_multiple_reservations_blank_separated() -> None:
    text = textwrap.dedent("""
        ReservationName=maint1 StartTime=2026-04-15T03:00:00 EndTime=2026-04-15T05:00:00
           Nodes=cn001 NodeCnt=1 Users=root

        ReservationName=maint2 StartTime=2026-04-16T03:00:00 EndTime=2026-04-16T05:00:00
           Nodes=cn002,cn003 NodeCnt=2 Users=root,alice
        """).strip()
    parsed = parse_slurm_reservations(text)
    assert [r.name for r in parsed] == ["maint1", "maint2"]
    assert parsed[1].nodes == ("cn002", "cn003")
    assert parsed[1].users == ("root", "alice")


def test_parse_reservation_handles_inline_record_separator() -> None:
    """Some SLURM versions emit two reservations on one logical chunk
    (no blank line). Splitting by ``ReservationName=`` boundary
    survives that shape."""
    text = (
        "ReservationName=a StartTime=2026-04-15T03:00:00 Nodes=cn001 "
        "ReservationName=b StartTime=2026-04-15T04:00:00 Nodes=cn002"
    )
    parsed = parse_slurm_reservations(text)
    assert {r.name for r in parsed} == {"a", "b"}


def test_parse_empty_input_returns_empty_list() -> None:
    assert parse_slurm_reservations("") == []
    assert parse_slurm_reservations("\n\n") == []


def test_parse_unknown_endtime_surfaces_as_none() -> None:
    """SLURM emits ``EndTime=Unknown`` for unbounded reservations.
    Parser maps to None; the planner's window check then treats it
    as "indefinite hold"."""
    text = "ReservationName=indef StartTime=2026-04-15T03:00:00 EndTime=Unknown Nodes=cn001"
    parsed = parse_slurm_reservations(text)
    assert parsed[0].end_iso is None


def test_null_users_field_yields_empty_tuple() -> None:
    text = "ReservationName=any StartTime=2026-04-15T03:00:00 EndTime=2026-04-15T05:00:00 Nodes=cn001 Users=(null)"
    parsed = parse_slurm_reservations(text)
    assert parsed[0].users == ()


# ─── reservations_active_at ────────────────────────────────────────────


def test_reservations_active_at_picks_only_overlapping() -> None:
    """A reservation gates a proposed StartTime iff the window covers it."""
    a = ReservationHold(
        name="past", start_iso="2026-04-15T03:00:00+00:00", end_iso="2026-04-15T05:00:00+00:00"
    )
    b = ReservationHold(
        name="future",
        start_iso="2026-04-15T10:00:00+00:00",
        end_iso="2026-04-15T12:00:00+00:00",
    )
    c = ReservationHold(
        name="now", start_iso="2026-04-15T06:00:00+00:00", end_iso="2026-04-15T08:00:00+00:00"
    )
    active = reservations_active_at([a, b, c], at_iso="2026-04-15T07:00:00+00:00")
    assert [r.name for r in active] == ["now"]


def test_reservations_active_at_treats_missing_endtime_as_unbounded() -> None:
    r = ReservationHold(name="indef", start_iso="2026-04-15T03:00:00+00:00", end_iso=None)
    active = reservations_active_at([r], at_iso="2099-01-01T00:00:00+00:00")
    assert [x.name for x in active] == ["indef"]


def test_held_node_set_flattens_across_reservations() -> None:
    a = ReservationHold(name="a", nodes=("cn001", "cn002"))
    b = ReservationHold(name="b", nodes=("cn002", "cn003"))
    assert held_node_set([a, b]) == {"cn001", "cn002", "cn003"}


# ─── sacctmgr QOS parser ───────────────────────────────────────────────


def test_parse_sacctmgr_qos_round_trips_standard_format() -> None:
    """sacctmgr -P show qos pipe-separated output."""
    text = textwrap.dedent("""
        Name|Priority|Flags|MaxJobsPU|MaxCPUsPU|MaxSubmitJobsPU
        normal|0||100|400|200
        debug|1000|||16|10
        """).strip()
    parsed = parse_sacctmgr_qos(text)
    assert set(parsed) == {"normal", "debug"}
    n = parsed["normal"]
    assert n.priority == 0
    assert n.max_jobs_per_user == 100
    assert n.max_cpus_per_user == 400
    assert n.max_submit_jobs_per_user == 200
    d = parsed["debug"]
    assert d.priority == 1000
    assert d.max_jobs_per_user is None  # empty cell → None
    assert d.max_cpus_per_user == 16


def test_parse_sacctmgr_qos_treats_negative_one_as_no_limit() -> None:
    """SLURM uses -1 to mean "no limit"; we surface that as None so
    callers don't accidentally interpret it as a real bound."""
    text = textwrap.dedent("""
        Name|MaxJobsPU
        unlim|-1
        """).strip()
    assert parse_sacctmgr_qos(text)["unlim"].max_jobs_per_user is None


def test_parse_sacctmgr_qos_column_order_independent() -> None:
    """Column order can vary across sacctmgr versions; the parser keys
    by name so a reordered header doesn't break."""
    text = textwrap.dedent("""
        MaxJobsPU|Name|Priority
        50|alpha|10
        """).strip()
    a = parse_sacctmgr_qos(text)["alpha"]
    assert a.max_jobs_per_user == 50
    assert a.priority == 10


def test_parse_sacctmgr_qos_missing_columns_yield_none() -> None:
    """A header that omits MaxCPUsPU still parses; the field is None."""
    text = textwrap.dedent("""
        Name|MaxJobsPU
        single|10
        """).strip()
    s = parse_sacctmgr_qos(text)["single"]
    assert s.max_cpus_per_user is None


def test_parse_sacctmgr_qos_skips_rows_with_no_name() -> None:
    text = textwrap.dedent("""
        Name|MaxJobsPU
        |10
        valid|5
        """).strip()
    assert set(parse_sacctmgr_qos(text)) == {"valid"}


def test_parse_sacctmgr_qos_empty_input_returns_empty_dict() -> None:
    assert parse_sacctmgr_qos("") == {}
    assert parse_sacctmgr_qos("Name|MaxJobsPU\n") == {}


def test_parse_sacctmgr_qos_no_name_column_returns_empty() -> None:
    """If Name isn't a column, we can't key the dict — give up cleanly."""
    text = textwrap.dedent("""
        Priority|Flags
        100|none
        """).strip()
    assert parse_sacctmgr_qos(text) == {}


def test_qos_limit_dataclass_is_frozen_and_hashable() -> None:
    """QosLimit being frozen (so it's safe to use as dict key /
    in a set) is part of the contract."""
    a = QosLimit(name="a", max_jobs_per_user=10)
    b = QosLimit(name="a", max_jobs_per_user=10)
    assert hash(a) == hash(b)
    assert a == b
