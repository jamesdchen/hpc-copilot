"""Fire-path tests for ``aggregate-stream`` (S-STREAM).

The verb is driven off two injection seams so no ssh/rsync runs: ``_census_fn``
(a fake that reuses the real ``census_arms`` with an in-memory done-set) and
``_pull_fn`` (a fake that stages per-task summary dirs into the mirror the reduce
scans).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent.ops.aggregate.arm_census import census_arms
from hpc_agent.ops.aggregate.stream import stream_aggregate
from hpc_agent.ops.monitor.announce import AnnouncedTaskIds
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord


def _seed_record(experiment: Path, run_id: str) -> None:
    upsert_run(
        experiment,
        RunRecord(
            run_id=run_id,
            profile="p",
            cluster="c",
            ssh_target="u@h",
            remote_path=f"/remote/{run_id}",
            job_name="p",
            job_ids=["job_1"],
            total_tasks=4,
            submitted_at="2026-01-01T00:00:00+00:00",
            experiment_dir=str(experiment.resolve()),
        ),
    )


def _seed_sidecar(experiment: Path, run_id: str, *, wave_map, task_count: int) -> None:
    from hpc_agent.state.runs import write_run_sidecar

    write_run_sidecar(
        experiment,
        run_id=run_id,
        cmd_sha="0" * 64,
        hpc_agent_version="0.2.0",
        submitted_at="2026-01-01T00:00:00Z",
        executor="python3 run.py",
        result_dir_template="results/task_{seed}",
        task_count=task_count,
        tasks_py_sha="1" * 64,
        wave_map=wave_map,
    )


def _make_census_fn(done_by_run: dict[str, set[int]]):
    """A census seam that reuses the REAL census logic with an in-memory done-set."""

    def _fn(*, ssh_target, remote_path, run_id, wave_map, total_tasks, owner_run_id=None, **_):
        done = done_by_run[run_id]

        def _reader(**_kw) -> AnnouncedTaskIds:
            return AnnouncedTaskIds(present=True, done_ids=frozenset(done))

        return census_arms(
            ssh_target=ssh_target,
            remote_path=remote_path,
            run_id=run_id,
            wave_map=wave_map,
            total_tasks=total_tasks,
            owner_run_id=owner_run_id,
            _read_ids=_reader,
        )

    return _fn


def _stage_task(mirror: Path, task_id: int, payload: dict) -> None:
    d = mirror / f"task_{task_id}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "metrics.json").write_text(json.dumps(payload), encoding="utf-8")


def _make_pull_fn(staged: dict[str, dict[int, dict]]):
    """A pull seam that stages the given per-run task summaries into each mirror.

    *staged* maps run_id → {task_id: metrics_payload}. The mirror dir ends in the
    run_id (``…/_stream_mirror/<run_id>``), so the fake keys off that.
    """

    def _pull(*, ssh_target, remote_path, remote_subdir, local_dir, include, **_):
        from subprocess import CompletedProcess

        mirror = Path(local_dir)
        run_id = mirror.name
        for tid, payload in staged.get(run_id, {}).items():
            _stage_task(mirror, tid, payload)
        return CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    return _pull


def _spec(**kw):
    from hpc_agent._wire.queries.stream_aggregate import StreamAggregateInput

    return StreamAggregateInput(**kw)


def test_stream_emits_complete_arms_and_discloses_pending(
    tmp_path: Path, journal_home: Path
) -> None:
    _seed_record(tmp_path, "r1")
    _seed_sidecar(tmp_path, "r1", wave_map={"0": [0, 1], "1": [2, 3]}, task_count=4)

    census_fn = _make_census_fn({"r1": {0, 1, 2}})  # wave 0 whole, wave 1 half
    pull_fn = _make_pull_fn(
        {"r1": {0: {"qlike": 0.1, "n_samples": 5}, 1: {"qlike": 0.3, "n_samples": 5}}}
    )
    res = stream_aggregate(
        tmp_path, spec=_spec(run_id="r1"), _census_fn=census_fn, _pull_fn=pull_fn
    )
    assert res.ok is True
    assert res.arms_complete == ["r1:0"]
    # pending arm disclosed BY NAME with progress + owner
    assert [p.arm for p in res.arms_pending] == ["1"]
    assert res.arms_pending[0].tasks_done == 1
    assert res.arms_pending[0].tasks_expected == 2
    assert res.arms_pending[0].owner_run_id == "r1"
    # every emitted number is reducer-computed: qlike = weighted mean of 0.1, 0.3
    assert res.aggregated_metrics["qlike"] == pytest.approx(0.2)
    assert res.aggregated_metrics["n_samples"] == 10
    assert "r1:0" in res.per_arm_metrics
    assert res.snapshot_seq == 1
    assert res.superseded is None
    # snapshot persisted at the canonical location carrying the disclosure block
    snap = json.loads(Path(res.output_path_local).read_text(encoding="utf-8"))
    assert snap["provenance"]["arms_pending"][0]["arm"] == "1"
    assert snap["provenance"]["source"] == "stream"


def test_second_call_refines_monotonically(tmp_path: Path, journal_home: Path) -> None:
    _seed_record(tmp_path, "r1")
    _seed_sidecar(tmp_path, "r1", wave_map={"0": [0, 1], "1": [2, 3]}, task_count=4)
    pull_fn = _make_pull_fn(
        {
            "r1": {
                0: {"n_samples": 1},
                1: {"n_samples": 1},
                2: {"n_samples": 1},
                3: {"n_samples": 1},
            }
        }
    )

    # First call: only wave 0 complete.
    r1 = stream_aggregate(
        tmp_path,
        spec=_spec(run_id="r1"),
        _census_fn=_make_census_fn({"r1": {0, 1}}),
        _pull_fn=pull_fn,
    )
    assert r1.snapshot_seq == 1
    assert r1.arms_complete == ["r1:0"]

    # Second call: wave 1 now also complete — refines, non-decreasing.
    r2 = stream_aggregate(
        tmp_path,
        spec=_spec(run_id="r1"),
        _census_fn=_make_census_fn({"r1": {0, 1, 2, 3}}),
        _pull_fn=pull_fn,
    )
    assert r2.snapshot_seq == 2
    assert r2.superseded == 1
    assert r2.arms_complete == ["r1:0", "r1:1"]
    assert r2.newly_complete == ["r1:1"]
    assert r2.arms_regressed == []


def test_zero_complete_refuses_with_pending_named(tmp_path: Path, journal_home: Path) -> None:
    _seed_record(tmp_path, "r1")
    _seed_sidecar(tmp_path, "r1", wave_map={"0": [0, 1]}, task_count=2)
    with pytest.raises(errors.PreconditionFailed, match="zero arms complete"):
        stream_aggregate(
            tmp_path,
            spec=_spec(run_id="r1"),
            _census_fn=_make_census_fn({"r1": {0}}),  # wave 0 half done
            _pull_fn=_make_pull_fn({}),
        )


def test_multi_parent_ownership_counts_shared_cell_once(
    tmp_path: Path, journal_home: Path
) -> None:
    """A cell the source finished AFTER the census (the qdel race) exists under
    BOTH run_ids' mirrors; the persisted ownership map drops it to its owner so
    its n is counted ONCE (composes migrate.harvest.multi_parent_reduce)."""
    from hpc_agent.ops.migrate.ownership import compute_ownership_map, persist_ownership_map

    _seed_record(tmp_path, "src")
    _seed_record(tmp_path, "der")
    _seed_sidecar(tmp_path, "src", wave_map={"0": [0, 1]}, task_count=2)
    _seed_sidecar(tmp_path, "der", wave_map={"0": [0]}, task_count=1)

    # Ownership: source-global cell 0 done (src owns), cell 1 undone (der owns);
    # der re-indexes cell 1 → local 0.
    om = compute_ownership_map(
        total=2, undone_ids=[1], done_ids=[0], source_run_id="src", derived_run_id="der"
    )
    persist_ownership_map(tmp_path, om)

    # source mirror carries task_0 (owned) AND task_1 (the RACED cell — owned by
    # der); derived mirror carries local task_0 (= source-global cell 1, owned).
    pull_fn = _make_pull_fn(
        {
            "src": {0: {"n_samples": 10}, 1: {"n_samples": 10}},
            "der": {0: {"n_samples": 10}},
        }
    )
    census_fn = _make_census_fn({"src": {0, 1}, "der": {0}})
    res = stream_aggregate(
        tmp_path, spec=_spec(parents=["src", "der"]), _census_fn=census_fn, _pull_fn=pull_fn
    )
    assert res.reduce_path == "ownership"
    # cell 1 present under both mirrors, dropped to its owner (der) → counted once.
    assert res.ownership_dedup is not None
    assert res.ownership_dedup["dropped_raced"] == [1]
    assert res.ownership_dedup["cells_counted"] == 2  # cells 0 and 1, each once
    # n summed across the 2 distinct owned cells (10 + 10), NOT 30 (no double count).
    assert res.aggregated_metrics["n_samples"] == 20


def test_exactly_one_target_enforced() -> None:
    with pytest.raises(ValueError, match="EXACTLY ONE"):
        _spec(run_id="r1", parents=["r2"])
    with pytest.raises(ValueError, match="EXACTLY ONE"):
        _spec()
