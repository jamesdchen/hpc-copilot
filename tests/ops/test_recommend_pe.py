"""Tests for ``recommend_pe`` — SGE parallel-environment auto-selection (#293).

Pins the selection rule: only ``source="pe"`` + ``kind="mpi"`` entries are
candidates; the tightest sufficient PE wins; insufficient capacity / no MPI PE
return ``None`` with a diagnostic rationale.
"""

from __future__ import annotations

from hpc_agent.ops.recommend_pe import recommend_pe


def _pe(name, kind, slots, source="pe"):
    return {"name": name, "source": source, "kind": kind, "raw": {"slots": slots}}


def test_tightest_fit_wins():
    pes = [_pe("big", "mpi", 256), _pe("small", "mpi", 32), _pe("smp", "smp", 16)]
    name, why = recommend_pe(pes, 16)
    assert name == "small"  # smallest mpi PE that holds 16, not the 256-slot one
    assert "tightest_fit" in why


def test_only_sufficient_capacity_is_picked():
    pes = [_pe("small", "mpi", 32), _pe("big", "mpi", 256)]
    name, _ = recommend_pe(pes, 100)
    assert name == "big"  # small (32) can't hold 100


def test_no_pe_fits_returns_none_with_largest():
    pes = [_pe("small", "mpi", 32), _pe("mid", "mpi", 64)]
    name, why = recommend_pe(pes, 500)
    assert name is None
    assert "no_pe_fits_ranks" in why and "largest_slots=64" in why


def test_smp_and_partitions_are_ignored():
    # Only source=pe + kind=mpi count; an smp PE or a SLURM partition tagged
    # kind=mpi is not a -pe target.
    pes = [
        _pe("smp", "smp", 999),
        _pe("part", "mpi", 999, source="partition"),
    ]
    name, why = recommend_pe(pes, 8)
    assert name is None
    assert why == "no_mpi_pe"


def test_unknown_slots_is_assumed_usable():
    # A PE whose slots didn't parse (None) is still a candidate — better to try
    # it than to refuse the submit on missing metadata.
    pes = [{"name": "mpi", "source": "pe", "kind": "mpi", "raw": {"slots": None}}]
    name, _ = recommend_pe(pes, 64)
    assert name == "mpi"


def test_known_capacity_preferred_over_unknown():
    pes = [
        {"name": "unknown", "source": "pe", "kind": "mpi", "raw": {"slots": None}},
        _pe("known", "mpi", 128),
    ]
    name, _ = recommend_pe(pes, 16)
    assert name == "known"  # a known-sufficient PE beats an unknown-capacity one


def test_deterministic_on_ties():
    pes = [_pe("b", "mpi", 64), _pe("a", "mpi", 64)]
    assert recommend_pe(pes, 8)[0] == "a"  # equal slots -> name tiebreak
