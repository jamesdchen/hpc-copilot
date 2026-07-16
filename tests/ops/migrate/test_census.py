"""M-CENSUS — the done-set census + wave-aligned remainder partition [SPEC §3 A].

Covers the four acceptance cases: id parse + undone remainder counted via fake
ssh; whole-wave alignment; the no-census REFUSE; and the surfaced (never-masked)
status-reporter disagreement — plus the index-bounded target REFUSE (the guard the
constraint requires a fire-path test for).
"""

from __future__ import annotations

import subprocess

import pytest

from hpc_agent import errors
from hpc_agent.ops.migrate.census import census_remainder
from hpc_agent.ops.monitor.announce import AnnouncedTaskIds


def _ids(*done: int, present: bool = True) -> AnnouncedTaskIds:
    return AnnouncedTaskIds(present=present, done_ids=frozenset(done))


def _reader(result: AnnouncedTaskIds):
    def _r(*, ssh_target: str, remote_path: str, run_id: str) -> AnnouncedTaskIds:
        return result

    return _r


# ── acceptance #1: done_ids={3,7}, total=10 → undone has 8 ids ─────────────────


def test_census_partitions_undone_remainder() -> None:
    res = census_remainder(
        ssh_target="u@h",
        remote_path="/remote/exp",
        source_run_id="r1",
        total_tasks=10,
        target_uses_global_array_index=True,
        _read_ids=_reader(_ids(3, 7)),
    )
    assert res.done_ids == (3, 7)
    assert res.undone_ids == (0, 1, 2, 4, 5, 6, 8, 9)
    assert res.undone_count == 8
    # No wave_map → not wave-aligned; the remainder is 3 disjoint windows.
    assert res.wave_aligned is False
    assert res.range_shape == "arbitrary"
    assert res.n_ranges == 3
    assert res.task_range == "0-2,4-6,8-9"


def test_census_via_fake_ssh_reader_default_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # The default reader path (no _read_ids override): fake the ssh layer that
    # read_announced_task_ids uses, so the undone remainder is "counted via fake ssh".
    from hpc_agent.ops.monitor import announce

    out = "__HPC_ANNOUNCE_IDS_ACK__\ntask_3.complete\ntask_7.complete\n"

    def _fake_ssh(*a, **k) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=out, stderr="")

    monkeypatch.setattr(announce.remote, "ssh_run", _fake_ssh)
    res = census_remainder(
        ssh_target="u@h",
        remote_path="/remote/exp",
        source_run_id="r1",
        total_tasks=10,
        target_uses_global_array_index=True,
    )
    assert res.undone_count == 8
    assert res.done_ids == (3, 7)


# ── acceptance #2: whole-wave alignment ───────────────────────────────────────


def test_whole_wave_alignment_emits_wave_range() -> None:
    # Bucket-major: 3 waves of whole 100-id ranges; wave 0 is done, waves 1+2 are
    # entirely undone → wave-aligned, whole_waves=[1,2], the wave's global range.
    wave_map = {
        "0": list(range(0, 100)),
        "1": list(range(100, 200)),
        "2": list(range(200, 300)),
    }
    res = census_remainder(
        ssh_target="u@h",
        remote_path="/r",
        source_run_id="r1",
        total_tasks=300,
        target_uses_global_array_index=False,  # index-bounded is FINE for whole waves
        wave_map=wave_map,
        _read_ids=_reader(_ids(*range(0, 100))),
    )
    assert res.wave_aligned is True
    assert res.whole_waves == (1, 2)
    assert res.range_shape == "wave_aligned"
    assert res.task_range == "100-299"
    assert res.undone_count == 200


def test_wave_split_remainder_is_not_aligned() -> None:
    # A remainder that splits a wave (wave 1 partly done) is NOT aligned — falls to
    # an arbitrary/contiguous id range, not a whole-wave unit.
    wave_map = {"0": list(range(0, 100)), "1": list(range(100, 200))}
    # done = all of wave 0 + half of wave 1 → wave 1 is split.
    res = census_remainder(
        ssh_target="u@h",
        remote_path="/r",
        source_run_id="r1",
        total_tasks=200,
        target_uses_global_array_index=True,
        wave_map=wave_map,
        _read_ids=_reader(_ids(*range(0, 150))),
    )
    assert res.wave_aligned is False
    assert res.whole_waves == ()
    assert res.task_range == "150-199"
    assert res.range_shape == "contiguous"  # single window, expressible anywhere


# ── acceptance #3: no census → REFUSE (not zero-undone) ───────────────────────


def test_absent_census_refuses_never_all_undone() -> None:
    with pytest.raises(errors.PreconditionFailed) as ei:
        census_remainder(
            ssh_target="u@h",
            remote_path="/r",
            source_run_id="r1",
            total_tasks=10,
            target_uses_global_array_index=True,
            _read_ids=_reader(_ids(present=False)),
        )
    assert "no per-task census" in str(ei.value)


def test_no_undone_tasks_refuses_route_to_aggregate() -> None:
    with pytest.raises(errors.PreconditionFailed) as ei:
        census_remainder(
            ssh_target="u@h",
            remote_path="/r",
            source_run_id="r1",
            total_tasks=4,
            target_uses_global_array_index=True,
            _read_ids=_reader(_ids(0, 1, 2, 3)),
        )
    assert "nothing to migrate" in str(ei.value)


# ── acceptance #4: reporter disagreement → surfaced, not masked ───────────────


def test_status_reporter_disagreement_is_surfaced() -> None:
    # Announce says {3,7} done; the reporter says {3,5} complete. The diff is
    # surfaced in both directions, never auto-masked.
    status_report = {
        "rows_observed_emitted": True,
        "tasks": {
            "3": {"status": "complete"},
            "5": {"status": "complete"},
        },
    }
    res = census_remainder(
        ssh_target="u@h",
        remote_path="/r",
        source_run_id="r1",
        total_tasks=10,
        target_uses_global_array_index=True,
        status_report=status_report,
        _read_ids=_reader(_ids(3, 7)),
    )
    assert res.disagreement == {"announce_only": [7], "reporter_only": [5]}
    assert res.digest()["disagreement"] == {"announce_only": [7], "reporter_only": [5]}


def test_status_reporter_agreement_leaves_no_disagreement() -> None:
    status_report = {
        "rows_observed_emitted": True,
        "tasks": {"3": {"status": "complete"}, "7": {"status": "complete"}},
    }
    res = census_remainder(
        ssh_target="u@h",
        remote_path="/r",
        source_run_id="r1",
        total_tasks=10,
        target_uses_global_array_index=True,
        status_report=status_report,
        _read_ids=_reader(_ids(3, 7)),
    )
    assert res.disagreement is None


# ── the index-bounded REFUSE (guard fire-path) ────────────────────────────────


def test_index_bounded_target_refuses_noncontiguous_remainder() -> None:
    # Non-contiguous remainder ({0-2,4-6,8-9}, 3 windows) + index-bounded target
    # (uses_global_array_index=False) → REFUSE, surfacing the range shape.
    with pytest.raises(errors.SpecInvalid) as ei:
        census_remainder(
            ssh_target="u@h",
            remote_path="/r",
            source_run_id="r1",
            total_tasks=10,
            target_uses_global_array_index=False,
            _read_ids=_reader(_ids(3, 7)),
        )
    msg = str(ei.value)
    assert "0-2,4-6,8-9" in msg and "index-bounded" in msg


def test_index_bounded_target_accepts_contiguous_remainder() -> None:
    # A single contiguous window IS expressible on an index-bounded backend
    # (LOCAL 1-N array + one offset) — no refuse.
    res = census_remainder(
        ssh_target="u@h",
        remote_path="/r",
        source_run_id="r1",
        total_tasks=10,
        target_uses_global_array_index=False,
        _read_ids=_reader(_ids(0, 1, 2, 3, 4)),
    )
    assert res.task_range == "5-9"
    assert res.range_shape == "contiguous"


def test_zero_total_tasks_refuses() -> None:
    with pytest.raises(errors.SpecInvalid):
        census_remainder(
            ssh_target="u@h",
            remote_path="/r",
            source_run_id="r1",
            total_tasks=0,
            target_uses_global_array_index=True,
            _read_ids=_reader(_ids()),
        )
