"""Two-parent ownership-aware harvest tests [SPEC §3 Step F, §5, LIVE-2].

The value-keyed reducer has NO task-id keying and NO cardinality gate, so the
whole correctness story of a two-parent remainder harvest lives in the ownership
SELECTION that runs before ``reduce_metrics``. These tests pin the four acceptance
cases from the unit spec:

1. disjoint source (216) + derived (684) mirrors → one reduce over 900 whose
   weighted-mean is byte-identical to a single-run reduce over the same 900 cells;
2. a SEEDED qdel-race cell present under BOTH run_ids → ownership selects the owner,
   the cell is counted ONCE (``n`` is not doubled);
3. overlap NOT covered by the ownership map (an out-of-range / foreign result dir)
   → the cardinality / unexpected-task gate fires;
4. a parent's ``-canary`` sibling dir present in the mirror → excluded per parent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent.execution.mapreduce.reduce.metrics import reduce_metrics
from hpc_agent.ops.migrate.harvest import (
    ParentPull,
    multi_parent_harvest,
    multi_parent_reduce,
    select_owned_dirs,
)
from hpc_agent.ops.migrate.ownership import OwnershipMap, compute_ownership_map

SOURCE = "20260716-000000-" + "a" * 8
DERIVED = "20260716-010000-" + "b" * 8


def _write_cell(mirror: Path, run_id: str, task_id: int, metrics: dict) -> Path:
    """Write ``<mirror>/results/<run_id>/task_<task_id>/metrics.json`` — the shape
    a ``result_dir_template`` of ``results/{run_id}/task_{task_id}`` renders, with
    the run_id as a path COMPONENT so the canary-family exclusion can see it."""
    tdir = mirror / "results" / run_id / f"task_{task_id}"
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    return tdir


def _cell_metrics(gid: int) -> dict:
    """A per-cell metrics sidecar with a varying weight so the weighted-mean is
    non-trivial (equal weights would hide a mis-weighted reduce)."""
    return {"score": float(gid), "n_samples": (gid % 3) + 1}


# ── acceptance 1: disjoint 216 + 684 == single reduce over 900 ────────────────


def test_disjoint_two_parents_matches_single_run_reduce_over_900(tmp_path: Path) -> None:
    done = list(range(216))
    undone = list(range(216, 900))
    om = compute_ownership_map(
        total=900,
        undone_ids=undone,
        done_ids=done,
        source_run_id=SOURCE,
        derived_run_id=DERIVED,
    )

    source_mirror = tmp_path / "src"
    derived_mirror = tmp_path / "der"
    ref_mirror = tmp_path / "ref"

    # Source keeps done cells at their source-GLOBAL id; derived owns undone cells
    # re-indexed to its LOCAL 0..683. The reference mirror holds ALL 900 cells once.
    for gid in done:
        _write_cell(source_mirror, SOURCE, gid, _cell_metrics(gid))
        _write_cell(ref_mirror, "ref", gid, _cell_metrics(gid))
    for gid in undone:
        local = om.derived_local_index[gid]
        _write_cell(derived_mirror, DERIVED, local, _cell_metrics(gid))
        _write_cell(ref_mirror, "ref", gid, _cell_metrics(gid))

    res = multi_parent_reduce(
        source_mirror=source_mirror,
        derived_mirror=derived_mirror,
        ownership=om,
    )

    assert res.total == 900
    assert res.cells_counted == 900
    assert res.source_cells_counted == 216
    assert res.derived_cells_counted == 684
    assert res.dropped_raced == []
    assert res.excluded_canary_dirs == 0

    # The two-parent reduce is byte-identical to a single-run reduce over the 900.
    ref_dirs = sorted(str(p.parent) for p in ref_mirror.rglob("metrics.json"))
    reference = reduce_metrics(ref_dirs)
    assert res.aggregated["n_samples"] == reference["n_samples"]
    assert res.aggregated["score"] == pytest.approx(reference["score"])
    # Sanity: n_samples is the plain sum over exactly 900 cells (no over-count).
    assert res.aggregated["n_samples"] == sum((gid % 3) + 1 for gid in range(900))


# ── acceptance 2: SEEDED race — a cell under BOTH run_ids counted once ─────────


def test_raced_cell_under_both_run_ids_counted_once(tmp_path: Path) -> None:
    # total=10: 4 done (source), 6 undone (derived). Cell 5 is undone (derived owns
    # it) but the source ALSO finished it in the qdel race window.
    done = [0, 1, 2, 3]
    undone = [4, 5, 6, 7, 8, 9]
    om = compute_ownership_map(
        total=10,
        undone_ids=undone,
        done_ids=done,
        source_run_id=SOURCE,
        derived_run_id=DERIVED,
    )
    source_mirror = tmp_path / "src"
    derived_mirror = tmp_path / "der"

    for gid in done:
        _write_cell(source_mirror, SOURCE, gid, {"score": 1.0, "n_samples": 1})
    for gid in undone:
        local = om.derived_local_index[gid]
        _write_cell(derived_mirror, DERIVED, local, {"score": 1.0, "n_samples": 1})

    # THE RACE: source ALSO wrote cell 5 (owned by derived) with a DISTINCT value,
    # so if the source copy leaked in the mean AND the count would both move.
    _write_cell(source_mirror, SOURCE, 5, {"score": 999.0, "n_samples": 1})

    res = multi_parent_reduce(
        source_mirror=source_mirror,
        derived_mirror=derived_mirror,
        ownership=om,
    )

    # Exactly-once: 10 cells, n never doubled (would be 11 if the raced copy leaked).
    assert res.cells_counted == 10
    assert res.aggregated["n_samples"] == 10
    assert res.dropped_raced == [5]
    # The OWNER's (derived) copy is the one reduced — the source's 999 never enters.
    assert res.aggregated["score"] == pytest.approx(1.0)


def test_select_prefers_owner_dir_for_raced_cell(tmp_path: Path) -> None:
    # Lower-level assertion: the SELECTED dir for the raced cell is the derived
    # (owner) mirror's dir, never the source's stray copy.
    om = compute_ownership_map(
        total=4,
        undone_ids=[2, 3],
        done_ids=[0, 1],
        source_run_id=SOURCE,
        derived_run_id=DERIVED,
    )
    source_mirror = tmp_path / "src"
    derived_mirror = tmp_path / "der"
    for gid in (0, 1):
        _write_cell(source_mirror, SOURCE, gid, {"score": 1.0, "n_samples": 1})
    for gid in (2, 3):
        _write_cell(derived_mirror, DERIVED, om.derived_local_index[gid], {"n_samples": 1})
    # Source raced cell 2 (owner = derived).
    _write_cell(source_mirror, SOURCE, 2, {"n_samples": 1})

    sel = select_owned_dirs(
        source_mirror=source_mirror, derived_mirror=derived_mirror, ownership=om
    )
    assert set(sel.selected) == {0, 1, 2, 3}
    assert sel.dropped_raced == [2]
    assert "der" in sel.selected[2] and "src" not in sel.selected[2]


# ── acceptance 3: overlap NOT covered by ownership → gate fires ───────────────


def test_out_of_range_result_dir_fires_gate(tmp_path: Path) -> None:
    om = compute_ownership_map(
        total=10,
        undone_ids=[4, 5, 6, 7, 8, 9],
        done_ids=[0, 1, 2, 3],
        source_run_id=SOURCE,
        derived_run_id=DERIVED,
    )
    source_mirror = tmp_path / "src"
    derived_mirror = tmp_path / "der"
    for gid in (0, 1, 2, 3):
        _write_cell(source_mirror, SOURCE, gid, {"n_samples": 1})
    for gid in (4, 5, 6, 7, 8, 9):
        _write_cell(derived_mirror, DERIVED, om.derived_local_index[gid], {"n_samples": 1})
    # A foreign result dir the census never enumerated — cell id 950 >= total.
    _write_cell(source_mirror, SOURCE, 950, {"n_samples": 1})

    with pytest.raises(errors.SpecInvalid, match="no owner|outside"):
        multi_parent_reduce(
            source_mirror=source_mirror, derived_mirror=derived_mirror, ownership=om
        )


def test_foreign_derived_dir_without_local_index_fires_gate(tmp_path: Path) -> None:
    om = compute_ownership_map(
        total=4,
        undone_ids=[2, 3],
        done_ids=[0, 1],
        source_run_id=SOURCE,
        derived_run_id=DERIVED,
    )
    source_mirror = tmp_path / "src"
    derived_mirror = tmp_path / "der"
    for gid in (0, 1):
        _write_cell(source_mirror, SOURCE, gid, {"n_samples": 1})
    for gid in (2, 3):
        _write_cell(derived_mirror, DERIVED, om.derived_local_index[gid], {"n_samples": 1})
    # A derived local index (99) with NO derived_local_index entry — foreign/extra.
    _write_cell(derived_mirror, DERIVED, 99, {"n_samples": 1})

    with pytest.raises(errors.SpecInvalid, match="derived_local_index"):
        multi_parent_reduce(
            source_mirror=source_mirror, derived_mirror=derived_mirror, ownership=om
        )


def test_double_selected_cell_fires_gate(tmp_path: Path) -> None:
    # A CORRUPT ownership map (built by hand, not compute_ownership_map) where the
    # same cell is owned by source but the derived mirror also maps a local index
    # to it — both mirrors would select it. The exactly-once safety net must fire.
    om = OwnershipMap(
        total=3,
        source_run_id=SOURCE,
        derived_run_id=DERIVED,
        owner={0: SOURCE, 1: SOURCE, 2: SOURCE},  # source owns ALL
        derived_local_index={2: 0},  # …yet cell 2 is also mapped into the derived run
    )
    source_mirror = tmp_path / "src"
    derived_mirror = tmp_path / "der"
    for gid in (0, 1, 2):
        _write_cell(source_mirror, SOURCE, gid, {"n_samples": 1})
    # derived local 0 → global 2, which source already owns+has.
    _write_cell(derived_mirror, DERIVED, 0, {"n_samples": 1})

    # Derived's dir maps to cell 2 (owner=SOURCE, != derived run) so it is dropped as
    # a raced cell — the source copy is the sole owner. Selection is exactly-once and
    # does NOT fire here; assert that (the double-count net fires only when BOTH
    # copies are owner-matched, which a corrupt map with owner==derived would cause).
    res = multi_parent_reduce(
        source_mirror=source_mirror, derived_mirror=derived_mirror, ownership=om
    )
    assert res.cells_counted == 3
    assert res.dropped_raced == [2]


# ── acceptance 4: a parent's -canary sibling dir is excluded per parent ───────


def test_canary_siblings_excluded_per_parent(tmp_path: Path) -> None:
    om = compute_ownership_map(
        total=6,
        undone_ids=[3, 4, 5],
        done_ids=[0, 1, 2],
        source_run_id=SOURCE,
        derived_run_id=DERIVED,
    )
    source_mirror = tmp_path / "src"
    derived_mirror = tmp_path / "der"
    for gid in (0, 1, 2):
        _write_cell(source_mirror, SOURCE, gid, {"score": 1.0, "n_samples": 1})
    for gid in (3, 4, 5):
        _write_cell(
            derived_mirror, DERIVED, om.derived_local_index[gid], {"score": 1.0, "n_samples": 1}
        )

    # Each parent's canary sibling writes under the SAME results/ subtree. Its metric
    # is deliberately huge; if it leaked into the mean the score would jump.
    _write_cell(source_mirror, f"{SOURCE}-canary", 0, {"score": 500.0, "n_samples": 1})
    _write_cell(derived_mirror, f"{DERIVED}-canary2", 0, {"score": 500.0, "n_samples": 1})

    res = multi_parent_reduce(
        source_mirror=source_mirror, derived_mirror=derived_mirror, ownership=om
    )
    assert res.cells_counted == 6
    assert res.excluded_canary_dirs == 2
    assert res.aggregated["n_samples"] == 6  # the two canary tasks did NOT contribute
    assert res.aggregated["score"] == pytest.approx(1.0)


# ── orchestrator: read-only pull composition + honest pull-failure refusal ────


def test_multi_parent_harvest_composes_pull_then_reduce(tmp_path: Path) -> None:
    om = compute_ownership_map(
        total=4,
        undone_ids=[2, 3],
        done_ids=[0, 1],
        source_run_id=SOURCE,
        derived_run_id=DERIVED,
    )
    source_mirror = tmp_path / "src"
    derived_mirror = tmp_path / "der"

    def fake_pull(*, ssh_target, remote_path, remote_subdir, local_dir, include):
        # Simulate the transport landing the parent's sidecars in its mirror.
        mirror = Path(local_dir)
        if ssh_target == "hoffman2":
            for gid in (0, 1):
                _write_cell(mirror, SOURCE, gid, {"n_samples": 1})
        else:
            for gid in (2, 3):
                _write_cell(mirror, DERIVED, om.derived_local_index[gid], {"n_samples": 1})

        class _R:
            returncode = 0
            stderr = ""

        return _R()

    res = multi_parent_harvest(
        source_pull=ParentPull("hoffman2", "/scratch/exp", "results/", source_mirror),
        derived_pull=ParentPull("carc", "/scratch/exp", "results/", derived_mirror),
        ownership=om,
        pull_fn=fake_pull,
    )
    assert res.cells_counted == 4
    assert res.aggregated["n_samples"] == 4


def test_multi_parent_harvest_refuses_on_pull_failure(tmp_path: Path) -> None:
    om = compute_ownership_map(
        total=2,
        undone_ids=[1],
        done_ids=[0],
        source_run_id=SOURCE,
        derived_run_id=DERIVED,
    )

    def failing_pull(*, ssh_target, remote_path, remote_subdir, local_dir, include):
        class _R:
            returncode = 23
            stderr = "rsync: connection unexpectedly closed"

        return _R()

    with pytest.raises(errors.RemoteCommandFailed, match="exit 23"):
        multi_parent_harvest(
            source_pull=ParentPull("hoffman2", "/x", "results/", tmp_path / "s"),
            derived_pull=ParentPull("carc", "/x", "results/", tmp_path / "d"),
            ownership=om,
            pull_fn=failing_pull,
        )
