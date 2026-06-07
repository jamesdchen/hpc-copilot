"""Composite tests for the #299 auto-resume auto-fire (``maybe_auto_resume``).

The pure gate (:func:`decide_auto_resume_from_ids`) is exhaustively covered
in ``test_auto_resume.py``. This file pins the *composite* that turns a
``"resume"`` verdict into an actual resubmit: both the cluster failure fetch
(authoritative ``preempted_task_ids``) and ``resubmit_flow`` are injected, so
these tests assert the wiring (which ids, which flags, the cap counter,
dedup) without touching a cluster.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hpc_agent import errors
from hpc_agent.ops.auto_resume_flow import maybe_auto_resume
from hpc_agent.state import run_record
from hpc_agent.state.journal import load_run, upsert_run
from hpc_agent.state.run_record import RunRecord

_RUN_ID = "20260606-120000-aaa"


@pytest.fixture
def journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home_hpc"
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", home)
    return home


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    d = tmp_path / "exp"
    d.mkdir()
    return d


def _seed_record(experiment_dir: Path, **overrides: Any) -> RunRecord:
    base: dict[str, Any] = {
        "run_id": _RUN_ID,
        "profile": "p",
        "cluster": "c",
        "ssh_target": "user@host",
        "remote_path": "/remote",
        "job_name": "myjob",
        "job_ids": ["9001"],
        "total_tasks": 4,
        "submitted_at": "2026-06-06T12:00:00+00:00",
        "experiment_dir": str(experiment_dir),
        "script": ".hpc/templates/cpu_array.sh",
        "backend": "slurm",
        "job_env": {"EXECUTOR": "python3 .hpc/_hpc_dispatch.py"},
        "auto_resume_on_kill": True,
        "max_auto_resumes": 2,
        "auto_resume_count": 0,
    }
    base.update(overrides)
    rec = RunRecord(**base)
    upsert_run(experiment_dir, rec)
    return rec


def _fetcher(preempted: list[int] | None):
    """Build a failures_fetcher stub returning *preempted* as the
    cluster-authoritative preempted_task_ids (omitted when None)."""

    def _fetch(*, experiment_dir: Path, run_id: str, **kw: Any) -> dict[str, Any]:
        out: dict[str, Any] = {"run_id": run_id, "failed_count": 0, "clusters": []}
        if preempted:
            out["preempted_count"] = len(preempted)
            out["preempted_task_ids"] = sorted(preempted)
        return out

    return _fetch


class _Recorder:
    """Records resubmit() calls and returns a stub result."""

    def __init__(self, *, deduped: bool = False, new_job_ids: list[str] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._deduped = deduped
        self._new_job_ids = new_job_ids or ["9100"]

    def __call__(self, experiment_dir: Path, run_id: str, **kwargs: Any) -> Any:
        self.calls.append({"experiment_dir": experiment_dir, "run_id": run_id, **kwargs})

        class _Result:
            deduped = self._deduped
            cluster_submitted = True
            new_job_ids = list(self._new_job_ids)

        return _Result()


# ── opt-in OFF (default) ──────────────────────────────────────────────────


def test_opt_in_off_never_resubmits(journal_home: Path, experiment: Path) -> None:
    _seed_record(experiment, auto_resume_on_kill=False)
    rec = _Recorder()
    fetch = _fetcher([0, 1])

    outcome = maybe_auto_resume(experiment, _RUN_ID, resubmit=rec, failures_fetcher=fetch)

    assert outcome.action == "escalate"
    assert "not enabled" in outcome.reason
    assert rec.calls == []
    assert load_run(experiment, _RUN_ID).auto_resume_count == 0


def test_opt_in_off_skips_the_cluster_fetch(journal_home: Path, experiment: Path) -> None:
    """The opt-out short-circuit must avoid the SSH round-trip entirely."""
    _seed_record(experiment, auto_resume_on_kill=False)

    def _boom(**kw: Any) -> dict[str, Any]:  # pragma: no cover - must not run
        raise AssertionError("failures_fetcher called for an opt-out run")

    outcome = maybe_auto_resume(experiment, _RUN_ID, resubmit=_Recorder(), failures_fetcher=_boom)
    assert outcome.action == "escalate"


# ── lean path: monitor supplies the fresh scheduler-side preempted ids ────


def test_supplied_ids_skip_the_cluster_fetch(journal_home: Path, experiment: Path) -> None:
    """When the monitor passes preempted_task_ids (folded from last_status by
    the status reporter, report-space/1-based), the composite resumes from them,
    does NOT fetch, and converts to 0-based HPC_TASK_ID for resubmit."""
    _seed_record(experiment)
    rec = _Recorder(new_job_ids=["9100"])

    def _boom(**kw: Any) -> dict[str, Any]:  # pragma: no cover - must not run
        raise AssertionError("failures_fetcher called despite supplied ids")

    outcome = maybe_auto_resume(
        experiment,
        _RUN_ID,
        preempted_task_ids=[1, 3],  # report-space (1-based array indices)
        resubmit=rec,
        failures_fetcher=_boom,
    )

    assert outcome.action == "resume"
    # Converted to 0-based HPC_TASK_ID (1->0, 3->2).
    assert outcome.task_ids == (0, 2)
    assert rec.calls[0]["failed_task_ids"] == [0, 2]
    assert load_run(experiment, _RUN_ID).auto_resume_count == 1


def test_empty_supplied_ids_falls_back_to_fetch(journal_home: Path, experiment: Path) -> None:
    """An empty/None supplied set means 'reporter found none' — fall back to the
    log-based fetch (cross-scheduler, e.g. SGE without exit codes)."""
    _seed_record(experiment)
    rec = _Recorder()
    fetch = _fetcher([1])  # report-space → 0-based [0]

    outcome = maybe_auto_resume(
        experiment,
        _RUN_ID,
        preempted_task_ids=[],  # falsy → fall back
        resubmit=rec,
        failures_fetcher=fetch,
    )

    assert outcome.action == "resume"
    assert outcome.task_ids == (0,)


# ── opt-in ON + preempted + under cap → resume ────────────────────────────


def test_resume_fires_with_exactly_preempted_ids(journal_home: Path, experiment: Path) -> None:
    _seed_record(experiment)
    rec = _Recorder(new_job_ids=["9100", "9101"])
    # Cluster-authoritative report (report-space, 1-based) says array indices
    # 1,3 were preempted → 0-based HPC_TASK_IDs 0,2 (the in-between task OOMed
    # and is absent from preempted_task_ids).
    fetch = _fetcher([1, 3])

    outcome = maybe_auto_resume(experiment, _RUN_ID, resubmit=rec, failures_fetcher=fetch)

    assert outcome.action == "resume"
    assert outcome.resubmitted is True
    assert outcome.task_ids == (0, 2)
    assert outcome.new_job_ids == ["9100", "9101"]

    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["failed_task_ids"] == [0, 2]
    assert call["category"] == "preempted"
    assert call["from_checkpoint"] is True
    assert call["submit_to_cluster"] is True
    assert call["bypass_preempt_throttle"] is True
    assert call["script"] == ".hpc/templates/cpu_array.sh"
    assert call["backend"] == "slurm"
    assert call["job_name"] == "myjob"
    assert call["job_env"] == {"EXECUTOR": "python3 .hpc/_hpc_dispatch.py"}

    assert load_run(experiment, _RUN_ID).auto_resume_count == 1
    assert outcome.auto_resume_count == 1


# ── OOM / executor error → escalate, never resubmit ───────────────────────


def test_oom_only_escalates_never_resubmits(journal_home: Path, experiment: Path) -> None:
    _seed_record(experiment)
    rec = _Recorder()
    # No preempted ids in the report (everything that failed was OOM/error).
    fetch = _fetcher([])

    outcome = maybe_auto_resume(experiment, _RUN_ID, resubmit=rec, failures_fetcher=fetch)

    assert outcome.action == "escalate"
    assert "not a resumable kill" in outcome.reason
    assert rec.calls == []
    assert load_run(experiment, _RUN_ID).auto_resume_count == 0


def test_preempt_then_oom_cycle_escalates_not_spins(journal_home: Path, experiment: Path) -> None:
    """Second-failure-is-OOM: the authoritative report reclassifies the task
    as OOM (absent from preempted_task_ids), so the composite escalates rather
    than re-resuming a stale preempt mark."""
    _seed_record(experiment, auto_resume_count=1)  # one prior resume already happened
    rec = _Recorder()
    fetch = _fetcher([])  # the resumed task OOMed → no longer "preempted"

    outcome = maybe_auto_resume(experiment, _RUN_ID, resubmit=rec, failures_fetcher=fetch)

    assert outcome.action == "escalate"
    assert "not a resumable kill" in outcome.reason
    assert rec.calls == []


# ── cap reached → escalate ────────────────────────────────────────────────


def test_cap_reached_escalates(journal_home: Path, experiment: Path) -> None:
    _seed_record(experiment, max_auto_resumes=2, auto_resume_count=2)
    rec = _Recorder()
    fetch = _fetcher([1])  # report-space → 0-based [0]; non-empty so cap gate fires

    outcome = maybe_auto_resume(experiment, _RUN_ID, resubmit=rec, failures_fetcher=fetch)

    assert outcome.action == "escalate"
    assert "cap reached (2/2)" in outcome.reason
    assert rec.calls == []
    assert load_run(experiment, _RUN_ID).auto_resume_count == 2


# ── cluster fetch failure → escalate gracefully (don't crash the monitor) ──


def test_fetch_error_escalates(journal_home: Path, experiment: Path) -> None:
    _seed_record(experiment)
    rec = _Recorder()

    def _boom(**kw: Any) -> dict[str, Any]:
        raise errors.SshUnreachable("ssh down")

    outcome = maybe_auto_resume(experiment, _RUN_ID, resubmit=rec, failures_fetcher=_boom)

    assert outcome.action == "escalate"
    assert "could not fetch cluster failures" in outcome.reason
    assert rec.calls == []


# ── distinct request_id per cap-loop attempt ──────────────────────────────


def test_request_id_distinct_per_attempt(journal_home: Path, experiment: Path) -> None:
    """Each fired resume folds the current count into the request_id so two
    genuine preemptions of the same set are NOT deduped against each other."""
    _seed_record(experiment, max_auto_resumes=5)
    rec = _Recorder()
    fetch = _fetcher([1, 2])

    maybe_auto_resume(experiment, _RUN_ID, resubmit=rec, failures_fetcher=fetch)
    maybe_auto_resume(experiment, _RUN_ID, resubmit=rec, failures_fetcher=fetch)

    assert rec.calls[0]["request_id"] != rec.calls[1]["request_id"]
    assert load_run(experiment, _RUN_ID).auto_resume_count == 2


# ── deduped replay does not consume a cap slot ────────────────────────────


def test_deduped_replay_does_not_increment_count(journal_home: Path, experiment: Path) -> None:
    _seed_record(experiment)
    rec = _Recorder(deduped=True)
    fetch = _fetcher([1, 2])

    outcome = maybe_auto_resume(experiment, _RUN_ID, resubmit=rec, failures_fetcher=fetch)

    assert outcome.action == "resume"
    assert outcome.resubmitted is False
    assert len(rec.calls) == 1
    assert load_run(experiment, _RUN_ID).auto_resume_count == 0


# ── no journal record → escalate gracefully ───────────────────────────────


def test_no_record_escalates(journal_home: Path, experiment: Path) -> None:
    rec = _Recorder()
    outcome = maybe_auto_resume(
        experiment, "nonexistent-run", resubmit=rec, failures_fetcher=_fetcher([0])
    )
    assert outcome.action == "escalate"
    assert "no journal record" in outcome.reason
    assert rec.calls == []
