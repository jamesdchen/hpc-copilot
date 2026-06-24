"""A5: regression — submit_and_record dedups via cmd_sha when the
journal has been wiped but the per-experiment sidecar at
``<exp>/.hpc/runs/<run_id>.json`` is still on disk.

Without the fallback path, the function would generate a fresh
RunRecord and the caller would re-submit a job the cluster already has
running.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from hpc_agent._wire.actions.submit import SubmitSpec as _WireSubmitSpec
from hpc_agent.ops.submit.runner import submit_and_record

if TYPE_CHECKING:
    from pathlib import Path


def _write_sidecar(experiment_dir: Path, run_id: str, **fields) -> Path:
    target = experiment_dir / ".hpc" / "runs" / f"{run_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "sidecar_schema_version": 2,
        "run_id": run_id,
        "cmd_sha": fields.pop("cmd_sha", "a" * 64),
        "hpc_agent_version": "0.2.0",
        "submitted_at": "2026-01-01T00:00:00Z",
        "executor": "python3 src/run.py",
        "result_dir_template": "results/{seed}",
        "task_count": fields.pop("task_count", 4),
        "tasks_py_sha": "1" * 64,
    }
    payload.update(fields)
    target.write_text(json.dumps(payload))
    return target


def test_cmd_sha_dedup_short_circuits_when_sidecar_exists(tmp_path: Path) -> None:
    """Journal is empty but sidecar with same cmd_sha exists -> dedup, no SSH."""
    cmd_sha = "f" * 64
    pre_existing_run_id = "20260101-000000-existin"

    _write_sidecar(
        tmp_path,
        pre_existing_run_id,
        cmd_sha=cmd_sha,
        profile="gpu-a100",
        cluster="discovery",
        ssh_target="me@cluster",
        remote_path="/scratch/exp",
        job_name="ml",
        job_ids=["12345"],
        task_count=4,
        campaign_id="",
    )

    record, deduped = submit_and_record(
        tmp_path,
        spec=_WireSubmitSpec(
            profile="gpu-a100",
            cluster="discovery",
            ssh_target="me@cluster",
            remote_path="/scratch/exp",
            job_name="ml",
            run_id="20260102-000000-newone1",  # different from sidecar
            job_ids=["99999"],  # would be different
            total_tasks=4,
        ),
        cmd_sha=cmd_sha,
    )

    assert deduped is True
    # Got the OLD run_id back, not the one we passed in, so the caller
    # will skip the qsub.
    assert record.run_id == pre_existing_run_id
    assert record.job_ids == ["12345"]


def test_cmd_sha_dedup_skips_same_campaign_iteration(tmp_path: Path) -> None:
    """A campaign-tagged submit must NOT dedup against a prior iteration of
    the same campaign even with identical cmd_sha — the iteration runs fresh
    (the stochastic-repeat footgun fix). Without campaign awareness this
    would short-circuit and silently drop the new trial."""
    cmd_sha = "e" * 64
    _write_sidecar(
        tmp_path,
        "20260101-000000-iter0001",
        cmd_sha=cmd_sha,
        job_ids=["12345"],
        campaign_id="tune",
    )

    record, deduped = submit_and_record(
        tmp_path,
        spec=_WireSubmitSpec(
            profile="cpu",
            cluster="discovery",
            ssh_target="me@cluster",
            remote_path="/scratch/exp",
            job_name="ml",
            run_id="20260102-000000-iter0002",
            job_ids=["99999"],
            total_tasks=4,
            campaign_id="tune",
        ),
        cmd_sha=cmd_sha,
    )

    assert deduped is False
    assert record.run_id == "20260102-000000-iter0002"


def test_cmd_sha_dedup_no_op_when_no_match(tmp_path: Path) -> None:
    """Sidecar with DIFFERENT cmd_sha must not short-circuit."""
    _write_sidecar(tmp_path, "20260101-000000-other00", cmd_sha="b" * 64)
    record, deduped = submit_and_record(
        tmp_path,
        spec=_WireSubmitSpec(
            profile="cpu",
            cluster="discovery",
            ssh_target="me@cluster",
            remote_path="/scratch/exp",
            job_name="ml",
            run_id="20260102-000000-fresh11",
            job_ids=["55555"],
            total_tasks=4,
        ),
        cmd_sha="c" * 64,  # mismatches the sidecar
    )
    assert deduped is False
    assert record.run_id == "20260102-000000-fresh11"


def test_cmd_sha_param_is_optional(tmp_path: Path) -> None:
    """Existing callers that do not pass cmd_sha must keep working."""
    record, deduped = submit_and_record(
        tmp_path,
        spec=_WireSubmitSpec(
            profile="cpu",
            cluster="discovery",
            ssh_target="me@cluster",
            remote_path="/scratch/exp",
            job_name="ml",
            run_id="20260102-000000-noshahere",
            job_ids=["7"],
            total_tasks=1,
        ),
    )
    assert deduped is False
    assert record.run_id == "20260102-000000-noshahere"


# ─── #207: code-iteration safety at the submit_and_record dedup gate ─────


def _spec(run_id: str, **kw: object) -> _WireSubmitSpec:
    """A minimal SubmitSpec; identical fields → identical experiment."""
    base = dict(
        profile="gpu-a100",
        cluster="discovery",
        ssh_target="me@cluster",
        remote_path="/scratch/exp",
        job_name="ml",
        run_id=run_id,
        job_ids=["99999"],
        total_tasks=4,
    )
    base.update(kw)
    return _WireSubmitSpec(**base)  # type: ignore[arg-type]


def test_207_default_dedups_against_stale_code(tmp_path: Path) -> None:
    """Default (lever off): same cmd_sha dedups against the prior run even
    when the recorded tasks_py_sha differs — params define the experiment,
    so the code edit replays the prior run BY DESIGN. We still get a drift
    warning (observability), but deduped=True and the OLD run_id wins."""
    cmd_sha = "f" * 64
    _write_sidecar(
        tmp_path,
        "20260101-000000-existin",
        cmd_sha=cmd_sha,
        tasks_py_sha="1" * 64,  # the code AT submit time
        job_ids=["12345"],
    )

    with pytest.warns(UserWarning, match="invalidate-on-code-change"):
        record, deduped = submit_and_record(
            tmp_path,
            spec=_spec("20260102-000000-newone1"),
            cmd_sha=cmd_sha,
            tasks_py_sha="2" * 64,  # the code AFTER an executor-body edit
            # invalidate_on_code_change defaults False
        )

    assert deduped is True
    assert record.run_id == "20260101-000000-existin"
    assert record.job_ids == ["12345"]


def test_207_opt_in_forces_fresh_run_on_code_change(tmp_path: Path) -> None:
    """Lever on: a code-only change (same cmd_sha, different tasks_py_sha)
    is NOT deduped — submit_and_record creates a fresh record with the
    caller's run_id so the new code actually runs."""
    cmd_sha = "f" * 64
    _write_sidecar(
        tmp_path,
        "20260101-000000-existin",
        cmd_sha=cmd_sha,
        tasks_py_sha="1" * 64,
        job_ids=["12345"],
    )

    record, deduped = submit_and_record(
        tmp_path,
        spec=_spec("20260102-000000-newone1"),
        cmd_sha=cmd_sha,
        tasks_py_sha="2" * 64,
        invalidate_on_code_change=True,
    )

    assert deduped is False
    assert record.run_id == "20260102-000000-newone1"  # the fresh run_id
    assert record.job_ids == ["99999"]  # the caller's job_ids, not the stale ones


def test_207_opt_in_still_dedups_when_code_unchanged(tmp_path: Path) -> None:
    """Lever on but the code is unchanged: ordinary param-and-code dedup —
    a transient-retry resubmit of the SAME code still short-circuits."""
    cmd_sha = "f" * 64
    _write_sidecar(
        tmp_path,
        "20260101-000000-existin",
        cmd_sha=cmd_sha,
        tasks_py_sha="1" * 64,
        job_ids=["12345"],
    )

    record, deduped = submit_and_record(
        tmp_path,
        spec=_spec("20260102-000000-newone1"),
        cmd_sha=cmd_sha,
        tasks_py_sha="1" * 64,  # SAME code as the recorded run
        invalidate_on_code_change=True,
    )

    assert deduped is True
    assert record.run_id == "20260101-000000-existin"
    assert record.job_ids == ["12345"]


# ─── #351 sub-bug #5: executor-command drift rides the #207 code-drift lane ──
#
# cmd_sha is PURE PARAMETER identity (#207) and the executor command is in NO
# identity sha at all — so a user who changes their entry point / executor and
# resubmits the SAME swept params would (pre-fix) silently REPLAY the old run on
# the PRE-change executor. These pin the three drift directions, mirroring the
# test_207_* tasks_py_sha tests above: opt-in → fresh, default → warn+dedup,
# same-executor → still dedups.


def test_351_opt_in_forces_fresh_run_on_executor_change(tmp_path: Path) -> None:
    """Lever on: same params + CHANGED executor (same tasks_py_sha) is NOT
    deduped — submit_and_record mints a fresh record with the caller's run_id
    so the NEW executor / entry point actually runs."""
    cmd_sha = "f" * 64
    _write_sidecar(
        tmp_path,
        "20260101-000000-existin",
        cmd_sha=cmd_sha,
        tasks_py_sha="1" * 64,
        executor="python3 src/run_v1.py --seed $SEED",  # the OLD entry point
        job_ids=["12345"],
    )

    record, deduped = submit_and_record(
        tmp_path,
        spec=_spec("20260102-000000-newone1"),
        cmd_sha=cmd_sha,
        tasks_py_sha="1" * 64,  # tasks.py UNCHANGED — only the executor moved
        current_executor="python3 src/run_v2.py --seed $SEED",  # the NEW one
        invalidate_on_code_change=True,
    )

    assert deduped is False
    assert record.run_id == "20260102-000000-newone1"  # fresh run, new executor
    assert record.job_ids == ["99999"]  # the caller's job_ids, not the stale ones


def test_351_default_warns_and_dedups_on_executor_change(tmp_path: Path) -> None:
    """Default (lever off): same params + changed executor still DEDUPS (params
    define the experiment, #207) BUT the drift is now VISIBLE — a UserWarning
    naming the executor change instead of the pre-fix SILENT replay. The win is
    observability; deduped stays True and the OLD run_id/job_ids win."""
    cmd_sha = "f" * 64
    _write_sidecar(
        tmp_path,
        "20260101-000000-existin",
        cmd_sha=cmd_sha,
        tasks_py_sha="1" * 64,
        executor="python3 src/run_v1.py --seed $SEED",
        job_ids=["12345"],
    )

    with pytest.warns(UserWarning, match="executor command"):
        record, deduped = submit_and_record(
            tmp_path,
            spec=_spec("20260102-000000-newone1"),
            cmd_sha=cmd_sha,
            tasks_py_sha="1" * 64,  # tasks.py unchanged
            current_executor="python3 src/run_v2.py --seed $SEED",
            # invalidate_on_code_change defaults False
        )

    assert deduped is True
    assert record.run_id == "20260101-000000-existin"
    assert record.job_ids == ["12345"]


def test_351_opt_in_still_dedups_when_executor_unchanged(tmp_path: Path) -> None:
    """Lever on but the executor is unchanged: NO false invalidation — a
    transient-retry resubmit of the SAME executor + SAME params still
    short-circuits (params-and-code-and-executor all match)."""
    cmd_sha = "f" * 64
    same_executor = "python3 src/run.py --seed $SEED"
    _write_sidecar(
        tmp_path,
        "20260101-000000-existin",
        cmd_sha=cmd_sha,
        tasks_py_sha="1" * 64,
        executor=same_executor,
        job_ids=["12345"],
    )

    record, deduped = submit_and_record(
        tmp_path,
        spec=_spec("20260102-000000-newone1"),
        cmd_sha=cmd_sha,
        tasks_py_sha="1" * 64,
        current_executor=same_executor,  # SAME executor as the recorded run
        invalidate_on_code_change=True,
    )

    assert deduped is True
    assert record.run_id == "20260101-000000-existin"
    assert record.job_ids == ["12345"]


# ─── #351 sub-bug #5: the guard at the find_run_by_cmd_sha unit boundary ────
#
# These pin the drift branch directly (no submit_and_record self-match noise):
# current_executor differs from the matched sidecar's recorded executor.


def test_351_find_run_opt_in_skips_match_on_executor_change(tmp_path: Path) -> None:
    """find_run_by_cmd_sha: same cmd_sha + same tasks_py_sha but a DIFFERENT
    current_executor than the recorded one, under invalidate_on_code_change →
    the match is NOT a replay target (returns None: no other sidecar matches)."""
    from hpc_agent.state.runs import find_run_by_cmd_sha

    cmd_sha = "f" * 64
    _write_sidecar(
        tmp_path,
        "20260101-000000-existin",
        cmd_sha=cmd_sha,
        tasks_py_sha="1" * 64,
        executor="python3 src/run_v1.py --seed $SEED",
        job_ids=["12345"],
    )

    hit = find_run_by_cmd_sha(
        tmp_path,
        cmd_sha,
        tasks_py_sha="1" * 64,  # tasks.py UNCHANGED — the executor alone moved
        current_executor="python3 src/run_v2.py --seed $SEED",
        invalidate_on_code_change=True,
    )
    assert hit is None  # drifted-executor match rejected → caller submits fresh


def test_351_find_run_default_warns_but_returns_match_on_executor_change(
    tmp_path: Path,
) -> None:
    """find_run_by_cmd_sha, lever off: an executor change still DEDUPS (returns
    the match) but emits the drift warning — observability without changing the
    dedup decision (the #207 default-dedup-with-warning contract, extended to
    the executor by #351)."""
    from hpc_agent.state.runs import find_run_by_cmd_sha

    cmd_sha = "f" * 64
    target = _write_sidecar(
        tmp_path,
        "20260101-000000-existin",
        cmd_sha=cmd_sha,
        tasks_py_sha="1" * 64,
        executor="python3 src/run_v1.py --seed $SEED",
        job_ids=["12345"],
    )

    with pytest.warns(UserWarning, match="executor command"):
        hit = find_run_by_cmd_sha(
            tmp_path,
            cmd_sha,
            tasks_py_sha="1" * 64,
            current_executor="python3 src/run_v2.py --seed $SEED",
            # invalidate_on_code_change defaults False
        )
    assert hit == target  # still deduped — only the visibility changed


def test_351_find_run_no_drift_when_recorded_executor_absent(tmp_path: Path) -> None:
    """A matched sidecar with NO recorded executor is NOT treated as drift
    (mirrors the empty-tasks_py_sha tolerance) — we cannot prove it changed, so
    the dedup falls back to param-only and neither warns nor invalidates."""
    import warnings

    from hpc_agent.state.runs import find_run_by_cmd_sha

    cmd_sha = "f" * 64
    target = _write_sidecar(
        tmp_path,
        "20260101-000000-existin",
        cmd_sha=cmd_sha,
        tasks_py_sha="1" * 64,
        executor="",  # absent/empty recorded executor disables the check
        job_ids=["12345"],
    )

    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any drift warning would fail the test
        hit = find_run_by_cmd_sha(
            tmp_path,
            cmd_sha,
            tasks_py_sha="1" * 64,
            current_executor="python3 src/run_v2.py --seed $SEED",
            invalidate_on_code_change=True,  # would invalidate IF drift fired
        )
    assert hit == target  # no drift detected → still a valid dedup target


# ─── #351 sub-bug #5 (layer-1 companion): the COMPLETE-prior in-place redo ───
#
# These pin the LAYER-1 dedup gate (the ``load_run(run_id)`` branch), NOT the
# A5/cmd_sha LAYER-2 scan the tests above exercise. The distinction: here the
# spec's run_id MATCHES a COMPLETE journal record (same swept params → same
# run_id), so ``load_run`` returns it and the COMPLETE-dedup short-circuits
# BEFORE layer 2 ever runs. A "redo this finished run with new code" (changed
# executor / entry point, unchanged params) was therefore a SILENT replay on
# the PRE-change executor — layer 2's #5 guard never got a chance to fire.
#
# The recorded prior executor lives on the JOURNAL RunRecord, not the sidecar:
# a same-run_id redo overwrites the sidecar with the NEW code at Step 6d before
# submit_and_record runs, so the journal copy is the only durable prior signal
# (see ``_layer1_code_drift``). These fixtures reproduce that exact on-disk
# state: a COMPLETE journal record carrying the OLD executor + a sidecar that
# already holds the NEW (about-to-submit) code.


def _seed_complete_journal_record(
    experiment_dir: Path,
    run_id: str,
    *,
    executor: str,
    tasks_py_sha: str = "1" * 64,
    job_ids: list[str] | None = None,
) -> None:
    """Write a COMPLETE journal RunRecord at *run_id* carrying the PRIOR run's
    executor / tasks_py_sha — the durable provenance the layer-1 redo gate
    compares against. ``HPC_JOURNAL_DIR`` must already point at a tmp dir."""
    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord

    upsert_run(
        experiment_dir,
        RunRecord(
            run_id=run_id,
            profile="gpu-a100",
            cluster="discovery",
            ssh_target="me@cluster",
            remote_path="/scratch/exp",
            job_name="ml",
            job_ids=list(job_ids or ["12345"]),
            total_tasks=4,
            submitted_at="2026-01-01T00:00:00Z",
            experiment_dir=str(experiment_dir),
            status="complete",
            executor=executor,
            tasks_py_sha=tasks_py_sha,
        ),
    )


def test_351_layer1_opt_in_redoes_complete_run_on_executor_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LAYER 1, lever ON: a COMPLETE prior run at the SAME run_id whose recorded
    executor differs from the about-to-submit one is NOT a replay — it falls
    through to a fresh IN-PLACE submit (same run_id, new executor). Pins the
    most common same-machine redo the layer-2 guard could never reach."""
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    run_id = "20260101-000000-redo001"
    cmd_sha = "f" * 64

    _seed_complete_journal_record(tmp_path, run_id, executor="python3 src/run_v1.py --seed $SEED")
    # The sidecar already holds the NEW code (Step 6d overwrote it pre-qsub →
    # no job_ids yet, i.e. an orphan, so the A5 scan after layer-1 invalidation
    # correctly falls through to a real submit rather than re-deduping on self).
    _write_sidecar(
        tmp_path,
        run_id,
        cmd_sha=cmd_sha,
        tasks_py_sha="1" * 64,
        executor="python3 src/run_v2.py --seed $SEED",
        job_ids=[],
    )

    record, deduped = submit_and_record(
        tmp_path,
        spec=_spec(run_id, job_ids=["99999"]),
        cmd_sha=cmd_sha,
        tasks_py_sha="1" * 64,  # tasks.py unchanged — only the executor moved
        current_executor="python3 src/run_v2.py --seed $SEED",
        invalidate_on_code_change=True,
    )

    assert deduped is False  # NOT a replay — the finished run is redone in place
    assert record.run_id == run_id  # SAME run_id (in-place; run-id minting untouched)
    assert record.job_ids == ["99999"]  # the fresh submission's job_ids
    assert record.status == "in_flight"  # a brand-new run, not the COMPLETE replay


def test_351_layer1_default_warns_and_dedups_complete_run_on_executor_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LAYER 1, lever OFF: a COMPLETE prior run with a changed executor STILL
    dedups (re-running a finished experiment stays an idempotent no-op) but the
    drift is now VISIBLE — a UserWarning naming the executor change instead of
    the pre-fix SILENT replay. The win is observability; the OLD run wins."""
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    run_id = "20260101-000000-redo002"
    cmd_sha = "f" * 64

    _seed_complete_journal_record(
        tmp_path,
        run_id,
        executor="python3 src/run_v1.py --seed $SEED",
        job_ids=["12345"],
    )
    _write_sidecar(
        tmp_path,
        run_id,
        cmd_sha=cmd_sha,
        executor="python3 src/run_v2.py --seed $SEED",
        job_ids=["12345"],
    )

    with pytest.warns(UserWarning, match="executor command"):
        record, deduped = submit_and_record(
            tmp_path,
            spec=_spec(run_id, job_ids=["99999"]),
            cmd_sha=cmd_sha,
            tasks_py_sha="1" * 64,
            current_executor="python3 src/run_v2.py --seed $SEED",
            # invalidate_on_code_change defaults False
        )

    assert deduped is True  # idempotent no-op preserved
    assert record.run_id == run_id
    assert record.job_ids == ["12345"]  # the COMPLETE run's job_ids, not ["99999"]
    assert record.status == "complete"  # the prior COMPLETE record is returned as-is


def test_351_layer1_same_executor_dedups_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LAYER 1: a COMPLETE prior run re-submitted with the SAME executor + SAME
    params dedups silently — NO false warning, NO false invalidation even under
    the opt-in lever (a transient-retry resubmit of an unchanged finished run
    must stay a clean idempotent no-op)."""
    import warnings

    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    run_id = "20260101-000000-redo003"
    cmd_sha = "f" * 64
    same_executor = "python3 src/run.py --seed $SEED"

    _seed_complete_journal_record(tmp_path, run_id, executor=same_executor, job_ids=["12345"])
    _write_sidecar(tmp_path, run_id, cmd_sha=cmd_sha, executor=same_executor, job_ids=["12345"])

    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any drift warning would fail the test
        record, deduped = submit_and_record(
            tmp_path,
            spec=_spec(run_id, job_ids=["99999"]),
            cmd_sha=cmd_sha,
            tasks_py_sha="1" * 64,
            current_executor=same_executor,  # unchanged
            invalidate_on_code_change=True,  # would invalidate IF drift fired
        )

    assert deduped is True  # clean idempotent no-op
    assert record.run_id == run_id
    assert record.job_ids == ["12345"]
    assert record.status == "complete"


def test_351_layer1_default_warns_and_dedups_complete_run_on_tasks_py_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LAYER 1 also covers tasks.py drift (the unified drift check): a COMPLETE
    prior run whose recorded tasks_py_sha differs from the current code warns +
    dedups by default — closing the SAME layer-1 bypass #207 had for executor
    drift. Same on-disk shape; only the tasks_py_sha moved this time."""
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    run_id = "20260101-000000-redo004"
    cmd_sha = "f" * 64
    same_executor = "python3 src/run.py --seed $SEED"

    _seed_complete_journal_record(
        tmp_path,
        run_id,
        executor=same_executor,
        tasks_py_sha="1" * 64,  # the code AT submit time
        job_ids=["12345"],
    )
    _write_sidecar(
        tmp_path,
        run_id,
        cmd_sha=cmd_sha,
        executor=same_executor,
        tasks_py_sha="2" * 64,
        job_ids=["12345"],
    )

    with pytest.warns(UserWarning, match="invalidate-on-code-change"):
        record, deduped = submit_and_record(
            tmp_path,
            spec=_spec(run_id, job_ids=["99999"]),
            cmd_sha=cmd_sha,
            tasks_py_sha="2" * 64,  # tasks.py drifted; executor unchanged
            current_executor=same_executor,
            # invalidate_on_code_change defaults False
        )

    assert deduped is True
    assert record.run_id == run_id
    assert record.job_ids == ["12345"]
