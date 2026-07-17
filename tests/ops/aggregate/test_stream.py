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
from hpc_agent.ops.aggregate.stream import aggregate_stream
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


def _seed_sidecar(
    experiment: Path,
    run_id: str,
    *,
    wave_map,
    task_count: int,
    aggregate_cmd: str | None = None,
    cmd_sha: str = "0" * 64,
) -> None:
    from hpc_agent.state.runs import write_run_sidecar

    write_run_sidecar(
        experiment,
        run_id=run_id,
        cmd_sha=cmd_sha,
        hpc_agent_version="0.2.0",
        submitted_at="2026-01-01T00:00:00Z",
        executor="python3 run.py",
        result_dir_template="results/task_{seed}",
        task_count=task_count,
        tasks_py_sha="1" * 64,
        wave_map=wave_map,
        aggregate_defaults={"aggregate_cmd": aggregate_cmd} if aggregate_cmd else None,
    )


def _make_reduce_fn(
    rows_by_arm: dict[tuple[str, str], dict], *, fail_arms: frozenset[str] = frozenset()
):
    """A cluster_reduce seam: returns the run's OWN reducer row per (run_id, arm).

    Records every invocation (run_id, arm, the HPC_STREAM_TASK_IDS allowlist) so a
    test can assert incomplete arms are never reduced and the memo skips re-reduce.
    An arm in *fail_arms* raises RemoteCommandFailed (the reducer-failure drill).
    """

    def _fn(
        experiment_dir, *, run_id, aggregate_cmd, output_path, local_dir, extra_env, timeout_sec
    ):
        arm = extra_env["HPC_STREAM_ARM"]
        _fn.calls.append((run_id, arm, extra_env["HPC_STREAM_TASK_IDS"]))
        if arm in fail_arms:
            raise errors.RemoteCommandFailed(f"reducer for arm {arm} exited 1: boom-{arm}")
        return {"ok": True, "reduced": rows_by_arm[(run_id, arm)], "exit_code": 0}

    _fn.calls = []  # type: ignore[attr-defined]
    return _fn


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
    from hpc_agent._wire.queries.aggregate_stream import AggregateStreamInput

    return AggregateStreamInput(**kw)


def test_stream_emits_complete_arms_and_discloses_pending(
    tmp_path: Path, journal_home: Path
) -> None:
    _seed_record(tmp_path, "r1")
    _seed_sidecar(tmp_path, "r1", wave_map={"0": [0, 1], "1": [2, 3]}, task_count=4)

    census_fn = _make_census_fn({"r1": {0, 1, 2}})  # wave 0 whole, wave 1 half
    pull_fn = _make_pull_fn(
        {"r1": {0: {"qlike": 0.1, "n_samples": 5}, 1: {"qlike": 0.3, "n_samples": 5}}}
    )
    res = aggregate_stream(
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
    r1 = aggregate_stream(
        tmp_path,
        spec=_spec(run_id="r1"),
        _census_fn=_make_census_fn({"r1": {0, 1}}),
        _pull_fn=pull_fn,
    )
    assert r1.snapshot_seq == 1
    assert r1.arms_complete == ["r1:0"]

    # Second call: wave 1 now also complete — refines, non-decreasing.
    r2 = aggregate_stream(
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
        aggregate_stream(
            tmp_path,
            spec=_spec(run_id="r1"),
            _census_fn=_make_census_fn({"r1": {0}}),  # wave 0 half done
            _pull_fn=_make_pull_fn({}),
        )


def test_multi_parent_ownership_counts_shared_cell_once(tmp_path: Path, journal_home: Path) -> None:
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
    res = aggregate_stream(
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


# ── custom-reducer streaming (s4-gaps item 5: the wrong-citable fix) ───────────


def test_custom_reducer_streams_run_own_values_only_for_complete_arms(
    tmp_path: Path, journal_home: Path
) -> None:
    """A run WITH aggregate_cmd streams the RUN'S OWN reducer per complete arm;
    the incomplete arm is never reduced, and no built-in mean is emitted."""
    _seed_record(tmp_path, "r1")
    _seed_sidecar(
        tmp_path,
        "r1",
        wave_map={"0": [0, 1], "1": [2, 3]},
        task_count=4,
        aggregate_cmd="python3 specs/reduce_qlike.py",
    )
    census_fn = _make_census_fn({"r1": {0, 1, 2}})  # arm 0 whole, arm 1 half
    reduce_fn = _make_reduce_fn({("r1", "0"): {"qlike": 0.123, "dm_better": True, "n": 100}})

    res = aggregate_stream(
        tmp_path, spec=_spec(run_id="r1"), _census_fn=census_fn, _reduce_fn=reduce_fn
    )

    assert res.reduce_path == "custom"
    # per-arm value is EXACTLY the run's own reducer output (not a weighted mean).
    assert res.per_arm_metrics["r1:0"] == {"qlike": 0.123, "dm_better": True, "n": 100}
    # cross-arm aggregation deferred to final harvest.
    assert res.aggregated_metrics == {}
    # incomplete arm 1 disclosed pending, NEVER reduced (only arm 0 was invoked).
    assert [p.arm for p in res.arms_pending] == ["1"]
    assert reduce_fn.calls == [("r1", "0", "0,1")]  # allowlist = arm 0's task ids
    # N-of-M labeling on the emission + per-arm-final labels on the snapshot.
    assert res.completeness_label == "1-of-2 arms complete"
    assert res.value_scope is not None
    snap = json.loads(Path(res.output_path_local).read_text(encoding="utf-8"))
    assert snap["provenance"]["completeness_label"] == "1-of-2 arms complete"
    assert snap["provenance"]["value_labels"]["per_arm_metrics"] == "per-arm-final"
    assert snap["provenance"]["arms_reduce_failed"] == []


def test_custom_reducer_memo_skips_re_reduce_and_cmd_sha_change_re_fires(
    tmp_path: Path, journal_home: Path
) -> None:
    """One reduce per newly-complete arm: a re-call hits the durable memo (no new
    invocation); a changed cmd_sha invalidates the receipt and re-fires."""
    _seed_record(tmp_path, "r1")
    _seed_sidecar(
        tmp_path,
        "r1",
        wave_map={"0": [0, 1], "1": [2, 3]},
        task_count=4,
        aggregate_cmd="python3 specs/reduce_qlike.py",
        cmd_sha="a" * 64,
    )
    reduce_fn = _make_reduce_fn({("r1", "0"): {"qlike": 0.1}})
    census_fn = _make_census_fn({"r1": {0, 1}})  # only arm 0 complete

    # First call reduces arm 0.
    r1 = aggregate_stream(
        tmp_path, spec=_spec(run_id="r1"), _census_fn=census_fn, _reduce_fn=reduce_fn
    )
    assert r1.per_arm_metrics["r1:0"] == {"qlike": 0.1}
    assert len(reduce_fn.calls) == 1

    # Second call, same cmd_sha → memo hit, NO new reduce invocation.
    r2 = aggregate_stream(
        tmp_path, spec=_spec(run_id="r1"), _census_fn=census_fn, _reduce_fn=reduce_fn
    )
    assert r2.per_arm_metrics["r1:0"] == {"qlike": 0.1}
    assert len(reduce_fn.calls) == 1  # unchanged — served from the receipt

    # Re-resolve the run (new cmd_sha) → the receipt key invalidates → re-fire.
    _seed_sidecar(
        tmp_path,
        "r1",
        wave_map={"0": [0, 1], "1": [2, 3]},
        task_count=4,
        aggregate_cmd="python3 specs/reduce_qlike.py",
        cmd_sha="b" * 64,
    )
    r3 = aggregate_stream(
        tmp_path, spec=_spec(run_id="r1"), _census_fn=census_fn, _reduce_fn=reduce_fn
    )
    assert r3.per_arm_metrics["r1:0"] == {"qlike": 0.1}
    assert len(reduce_fn.calls) == 2  # cmd_sha moved → arm 0 reduced again


def test_custom_reducer_failure_arm_disclosed_others_continue(
    tmp_path: Path, journal_home: Path
) -> None:
    """A reducer failure on one complete arm is disclosed VERBATIM; the other
    complete arms still stream, and the failed arm gets NO built-in fallback."""
    _seed_record(tmp_path, "r1")
    _seed_sidecar(
        tmp_path,
        "r1",
        wave_map={"0": [0, 1], "1": [2, 3]},
        task_count=4,
        aggregate_cmd="python3 specs/reduce_qlike.py",
    )
    census_fn = _make_census_fn({"r1": {0, 1, 2, 3}})  # both arms complete
    reduce_fn = _make_reduce_fn({("r1", "1"): {"qlike": 0.2}}, fail_arms=frozenset({"0"}))

    res = aggregate_stream(
        tmp_path, spec=_spec(run_id="r1"), _census_fn=census_fn, _reduce_fn=reduce_fn
    )

    assert res.ok is True  # never aborts
    # arm 1 streamed; arm 0 NOT present (no silent built-in number for it).
    assert res.per_arm_metrics == {"r1:1": {"qlike": 0.2}}
    assert [f.arm for f in res.arms_reduce_failed] == ["0"]
    assert "boom-0" in res.arms_reduce_failed[0].error  # verbatim reducer error
    snap = json.loads(Path(res.output_path_local).read_text(encoding="utf-8"))
    assert snap["provenance"]["arms_reduce_failed"][0]["arm"] == "0"
    assert "boom-0" in snap["provenance"]["arms_reduce_failed"][0]["error"]


def test_builtin_path_byte_unchanged_for_non_custom_run(tmp_path: Path, journal_home: Path) -> None:
    """A run WITHOUT aggregate_cmd keeps the built-in weighted-mean path, and the
    snapshot provenance carries NONE of the custom-only disclosure keys."""
    _seed_record(tmp_path, "r1")
    _seed_sidecar(tmp_path, "r1", wave_map={"0": [0, 1], "1": [2, 3]}, task_count=4)
    census_fn = _make_census_fn({"r1": {0, 1}})
    pull_fn = _make_pull_fn(
        {"r1": {0: {"qlike": 0.1, "n_samples": 5}, 1: {"qlike": 0.3, "n_samples": 5}}}
    )

    res = aggregate_stream(
        tmp_path, spec=_spec(run_id="r1"), _census_fn=census_fn, _pull_fn=pull_fn
    )

    assert res.reduce_path == "builtin"
    assert res.aggregated_metrics["qlike"] == pytest.approx(0.2)  # built-in mean intact
    assert res.arms_reduce_failed == []
    snap = json.loads(Path(res.output_path_local).read_text(encoding="utf-8"))
    prov = snap["provenance"]
    # the byte-shape pin: custom-only keys must NOT appear on the built-in path.
    assert "completeness_label" not in prov
    assert "value_scope" not in prov
    assert "value_labels" not in prov
    assert "arms_reduce_failed" not in prov
    assert set(prov) == {
        "source",
        "reduced_at",
        "parents",
        "arms_complete",
        "arms_pending",
        "snapshot_seq",
        "superseded",
        "newly_complete",
        "arms_regressed",
        "reduce_path",
        "ownership_dedup",
        "disagreement",
    }
