"""Ownership-map unit tests [SPEC §3 Step B.4, §5, LIVE-2].

The exactly-once cell → owning-run partition is the whole safety story for the
two-parent harvest: the value-keyed reducer has no cardinality gate, so a
non-partition here would surface only as a silently double- or under-counted ``n``.
These tests pin the partition guards, the derived re-index, the digest, and the
round-trip through the migrate-scoped artifact.
"""

from __future__ import annotations

import json

import pytest

from hpc_agent import errors
from hpc_agent.ops.migrate.ownership import (
    OWNERSHIP_SCHEMA_VERSION,
    compute_ownership_map,
    load_ownership_map,
    ownership_artifact_path,
    persist_ownership_map,
)

SOURCE = "20260716-000000-" + "a" * 8
DERIVED = "20260716-010000-" + "b" * 8


def test_partition_covers_range_exactly_once() -> None:
    # 900 cells, bucket-major: first 216 done, remaining 684 undone (the live case).
    done = list(range(216))
    undone = list(range(216, 900))
    om = compute_ownership_map(
        total=900,
        undone_ids=undone,
        done_ids=done,
        source_run_id=SOURCE,
        derived_run_id=DERIVED,
    )
    # Every undone cell → derived; every done cell → source.
    assert all(om.owner_of(c) == DERIVED for c in undone)
    assert all(om.owner_of(c) == SOURCE for c in done)
    # Union covers all 900 exactly once — no cell owned twice, none unowned.
    assert set(om.owner) == set(range(900))
    assert len(om.owner) == 900
    dig = om.digest()
    assert dig["source_cells"] == 216
    assert dig["derived_cells"] == 684
    assert dig["exactly_once"] is True


def test_derived_local_index_reindexes_undone_in_ascending_order() -> None:
    om = compute_ownership_map(
        total=10,
        undone_ids=[9, 3, 7],  # deliberately unsorted input
        done_ids=[0, 1, 2, 4, 5, 6, 8],
        source_run_id=SOURCE,
        derived_run_id=DERIVED,
    )
    # Undone cells re-index from 0 in ascending source-global-id order.
    assert om.derived_local_index == {3: 0, 7: 1, 9: 2}
    # Done cells are absent from the derived re-index (source owns them in place).
    assert 0 not in om.derived_local_index


def test_overlap_cell_in_both_sets_refuses() -> None:
    with pytest.raises(errors.SpecInvalid, match="BOTH"):
        compute_ownership_map(
            total=10,
            undone_ids=[3, 4, 5],
            done_ids=[3, 6, 7, 8, 9, 0, 1, 2],  # 3 in both
            source_run_id=SOURCE,
            derived_run_id=DERIVED,
        )


def test_uncovered_cell_refuses() -> None:
    with pytest.raises(errors.SpecInvalid, match="NEITHER"):
        compute_ownership_map(
            total=10,
            undone_ids=[3, 4],
            done_ids=[0, 1, 2],  # 5,6,7,8,9 unaccounted
            source_run_id=SOURCE,
            derived_run_id=DERIVED,
        )


def test_cell_outside_range_refuses() -> None:
    with pytest.raises(errors.SpecInvalid, match="outside"):
        compute_ownership_map(
            total=5,
            undone_ids=[7],  # 7 >= total
            done_ids=[0, 1, 2, 3, 4],
            source_run_id=SOURCE,
            derived_run_id=DERIVED,
        )


def test_duplicate_ids_refuse() -> None:
    with pytest.raises(errors.SpecInvalid, match="duplicates"):
        compute_ownership_map(
            total=5,
            undone_ids=[3, 3],
            done_ids=[0, 1, 2, 4],
            source_run_id=SOURCE,
            derived_run_id=DERIVED,
        )


def test_owner_of_unknown_cell_refuses() -> None:
    om = compute_ownership_map(
        total=4,
        undone_ids=[2, 3],
        done_ids=[0, 1],
        source_run_id=SOURCE,
        derived_run_id=DERIVED,
    )
    with pytest.raises(errors.SpecInvalid, match="no owner"):
        om.owner_of(99)


def test_persist_and_load_round_trip(tmp_path) -> None:
    om = compute_ownership_map(
        total=6,
        undone_ids=[4, 5],
        done_ids=[0, 1, 2, 3],
        source_run_id=SOURCE,
        derived_run_id=DERIVED,
    )
    path = persist_ownership_map(tmp_path, om)
    assert path == ownership_artifact_path(tmp_path, DERIVED)
    assert path.is_file()
    # Migrate-scoped artifact, NOT a sidecar write.
    assert path.parent.name == DERIVED
    assert path.parent.parent.name == "migrate"

    obj = json.loads(path.read_text(encoding="utf-8"))
    assert obj["schema"] == OWNERSHIP_SCHEMA_VERSION
    assert obj["total"] == 6
    assert "folds into the run sidecar" in obj["folds_into_sidecar"]

    back = load_ownership_map(tmp_path, DERIVED)
    assert back.owner == om.owner
    assert back.derived_local_index == om.derived_local_index
    assert back.total == om.total


def test_load_missing_artifact_raises(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        load_ownership_map(tmp_path, "does-not-exist")
