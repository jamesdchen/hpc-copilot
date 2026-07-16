"""Tests for ``hpc_agent.ops.aggregate.arm_census`` (S-CENSUS).

The census does at most ONE bounded ssh read (``read_announced_task_ids``); we
inject a fake ``_read_ids`` returning an ``AnnouncedTaskIds`` so no ssh runs. The
wave_map is the declared arm grouping (v1 wave-aligned-only, SPEC §8).
"""

from __future__ import annotations

import pytest

from hpc_agent import errors
from hpc_agent.ops.aggregate.arm_census import census_arms
from hpc_agent.ops.monitor.announce import AnnouncedTaskIds


def _fake_reader(done_ids: set[int], present: bool = True):
    def _read(*, ssh_target: str, remote_path: str, run_id: str) -> AnnouncedTaskIds:
        return AnnouncedTaskIds(present=present, done_ids=frozenset(done_ids))

    return _read


def test_wave_aligned_census_splits_complete_and_pending() -> None:
    """Two waves of 3 tasks; wave 0 fully done, wave 1 half done → wave 0 COMPLETE,
    wave 1 PENDING carrying tasks_done/tasks_expected (the whole-arm n-guard)."""
    census = census_arms(
        ssh_target="u@h",
        remote_path="/x",
        run_id="r1",
        wave_map={"0": [0, 1, 2], "1": [3, 4, 5]},
        total_tasks=6,
        _read_ids=_fake_reader({0, 1, 2, 3}),  # wave 0 whole + one task of wave 1
    )
    assert census.present is True
    assert [a.arm for a in census.complete_arms] == ["0"]
    pend = census.pending_arms
    assert [a.arm for a in pend] == ["1"]
    assert pend[0].tasks_done == 1
    assert pend[0].tasks_expected == 3
    assert pend[0].owner_run_id == "r1"
    # by-name disclosure row shape
    assert census.digest()["arms_pending"] == [
        {"arm": "1", "tasks_done": 1, "tasks_expected": 3, "owner_run_id": "r1"}
    ]


def test_all_arms_complete() -> None:
    census = census_arms(
        ssh_target="u@h",
        remote_path="/x",
        run_id="r1",
        wave_map={"0": [0, 1], "1": [2, 3]},
        total_tasks=4,
        _read_ids=_fake_reader({0, 1, 2, 3}),
    )
    assert [a.arm for a in census.complete_arms] == ["0", "1"]
    assert census.pending_arms == ()


def test_owner_run_id_threaded_for_multi_leg() -> None:
    census = census_arms(
        ssh_target="u@h",
        remote_path="/x",
        run_id="lgbm",
        wave_map={"0": [0, 1]},
        total_tasks=2,
        owner_run_id="lgbm-leg",
        _read_ids=_fake_reader(set()),
    )
    assert census.pending_arms[0].owner_run_id == "lgbm-leg"


def test_no_wave_map_refuses_final_harvest_only() -> None:
    with pytest.raises(errors.SpecInvalid, match="final harvest only"):
        census_arms(
            ssh_target="u@h",
            remote_path="/x",
            run_id="r1",
            wave_map=None,
            total_tasks=4,
            _read_ids=_fake_reader({0, 1, 2, 3}),
        )


def test_wave_map_gap_refuses_non_aligned() -> None:
    """wave_map covers {0,1,3} but total is 4 (id 2 belongs to no wave) → the
    partition is not clean → refuse (a non-wave-aligned run)."""
    with pytest.raises(errors.SpecInvalid, match="does not cleanly partition"):
        census_arms(
            ssh_target="u@h",
            remote_path="/x",
            run_id="r1",
            wave_map={"0": [0, 1], "1": [3]},
            total_tasks=4,
            _read_ids=_fake_reader({0, 1, 3}),
        )


def test_wave_map_overlap_refuses() -> None:
    with pytest.raises(errors.SpecInvalid, match="not a partition"):
        census_arms(
            ssh_target="u@h",
            remote_path="/x",
            run_id="r1",
            wave_map={"0": [0, 1], "1": [1, 2, 3]},
            total_tasks=4,
            _read_ids=_fake_reader({0, 1, 2, 3}),
        )


def test_non_integer_wave_key_refuses() -> None:
    with pytest.raises(errors.SpecInvalid, match="non-integer wave key"):
        census_arms(
            ssh_target="u@h",
            remote_path="/x",
            run_id="r1",
            wave_map={"all_features": [0, 1]},
            total_tasks=2,
            _read_ids=_fake_reader({0, 1}),
        )


def test_absent_census_refuses_never_all_undone() -> None:
    """present=False (no announce dir / dropped ack) REFUSES — never read as
    'every arm undone' (Δ1)."""
    with pytest.raises(errors.PreconditionFailed, match="no per-task census present"):
        census_arms(
            ssh_target="u@h",
            remote_path="/x",
            run_id="r1",
            wave_map={"0": [0, 1]},
            total_tasks=2,
            _read_ids=_fake_reader(set(), present=False),
        )


def test_total_tasks_nonpositive_refuses() -> None:
    with pytest.raises(errors.SpecInvalid, match="total_tasks must be positive"):
        census_arms(
            ssh_target="u@h",
            remote_path="/x",
            run_id="r1",
            wave_map={"0": [0]},
            total_tasks=0,
            _read_ids=_fake_reader({0}),
        )


def test_status_report_disagreement_surfaced() -> None:
    """A reporter that says task 2 complete while announce does not → the
    disagreement is surfaced, never auto-masked."""
    # Minimal status-report shape rows_observed_from_report understands: it reads
    # tasks.<id>.state == 'complete'. Build one that marks tasks 0,1,2 complete.
    status_report = {
        "tasks": {
            "0": {"status": "complete"},
            "1": {"status": "complete"},
            "2": {"status": "complete"},
        }
    }
    census = census_arms(
        ssh_target="u@h",
        remote_path="/x",
        run_id="r1",
        wave_map={"0": [0, 1], "1": [2, 3]},
        total_tasks=4,
        status_report=status_report,
        _read_ids=_fake_reader({0, 1}),  # announce says only 0,1 done
    )
    assert census.disagreement is not None
    assert census.disagreement["reporter_only"] == [2]
