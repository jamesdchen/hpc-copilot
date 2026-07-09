"""Tests for the ``reconcile-stale`` primitive (queue item 11, bulk-close seat).

The stale-in-flight class: N runs died with a revoked cluster account and still
read ``in_flight``. ``reconcile-stale`` issues ONE scheduler query per login
node (via ``batch-status``) and closes every run the scheduler no longer knows
through the EXISTING settle classification (``abandoned``/no-evidence) — never
one-by-one SSH, never a cluster action. Records the scheduler still knows, and
anything unverifiable, stay open and are listed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent.ops.monitor import reconcile_stale as mod
from hpc_agent.ops.monitor.reconcile_stale import reconcile_stale
from hpc_agent.state.journal import load_run, upsert_run
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from pathlib import Path

_NOW = "2026-07-09T00:00:00+00:00"
_OLD = "2026-06-01T00:00:00+00:00"  # weeks before _NOW


def _record(run_id: str, *, job_ids, submitted_at: str = _OLD, cluster="hoffman2") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        profile="p",
        cluster=cluster,
        ssh_target="u@h",
        remote_path="/remote",
        job_name="j",
        job_ids=list(job_ids),
        total_tasks=4,
        submitted_at=submitted_at,
        experiment_dir="/exp",
        status="in_flight",
    )


def _status(experiment_dir: Path, run_id: str) -> str:
    """The on-disk journal status for *run_id* (asserts the record exists)."""
    rec = load_run(experiment_dir, run_id)
    assert rec is not None, f"record {run_id} vanished"
    return rec.status


def _stub_batch(monkeypatch, *, runs, skipped=(), queries=1):
    """Replace the module's ``batch_status`` with a call-counting stub."""
    calls: list[dict] = []

    def _fake(*, experiment_dir):
        calls.append({"experiment_dir": experiment_dir})
        return {"runs": dict(runs), "skipped": list(skipped), "queries": queries}

    monkeypatch.setattr(mod, "batch_status", _fake)
    return calls


def test_all_scheduler_unknown_closed_abandoned_one_batch_call(tmp_path: Path, monkeypatch) -> None:
    """N in-flight runs the scheduler knows nothing of → all abandoned, ONE batch call."""
    for i in range(3):
        upsert_run(tmp_path, _record(f"r{i}", job_ids=[str(100 + i)]))

    runs = {f"r{i}": {"job_states": {}, "missing_job_ids": [str(100 + i)]} for i in range(3)}
    calls = _stub_batch(monkeypatch, runs=runs, queries=1)

    out = reconcile_stale(experiment_dir=tmp_path, now=_NOW)

    assert len(calls) == 1, "must issue exactly ONE batch-status call for all runs"
    assert out["examined"] == 3
    assert out["queries"] == 1
    assert out["closed_count"] == 3
    assert out["closed_by_class"] == {"abandoned": 3}
    assert out["left_open"] == []
    # Every record is now terminal-abandoned on disk with the settle reason.
    for i in range(3):
        rec = load_run(tmp_path, f"r{i}")
        assert rec is not None
        assert rec.status == "abandoned"
        assert rec.last_status["verdict_reason"] == "no_on_disk_evidence"
        assert rec.last_status["closed_by"] == "reconcile-stale"


def test_scheduler_known_run_left_open(tmp_path: Path, monkeypatch) -> None:
    """A run the scheduler still knows a job for stays in_flight, listed in left_open."""
    upsert_run(tmp_path, _record("alive", job_ids=["7"]))
    runs = {"alive": {"job_states": {"7": "running"}, "missing_job_ids": []}}
    _stub_batch(monkeypatch, runs=runs, queries=1)

    out = reconcile_stale(experiment_dir=tmp_path, now=_NOW)

    assert out["closed_count"] == 0
    assert out["left_open"] == [{"run_id": "alive", "reason": "scheduler_still_knows"}]
    assert _status(tmp_path, "alive") == "in_flight"


def test_no_job_ids_old_closed_recent_open(tmp_path: Path, monkeypatch) -> None:
    """A jobless record older than the threshold closes; a recent one stays open."""
    upsert_run(tmp_path, _record("old", job_ids=[], submitted_at=_OLD))
    upsert_run(tmp_path, _record("recent", job_ids=[], submitted_at=_NOW))
    # batch-status skips both (no job_ids to query) — nothing in ``runs``.
    _stub_batch(
        monkeypatch,
        runs={},
        skipped=[
            {"run_id": "old", "reason": "no_job_ids"},
            {"run_id": "recent", "reason": "no_job_ids"},
        ],
        queries=0,
    )

    out = reconcile_stale(experiment_dir=tmp_path, now=_NOW, stale_after_hours=24)

    assert out["closed_by_class"] == {"abandoned": 1}
    assert {c["run_id"] for c in out["closed"]} == {"old"}
    assert out["left_open"] == [{"run_id": "recent", "reason": "no_job_ids_too_recent"}]
    assert _status(tmp_path, "old") == "abandoned"
    assert _status(tmp_path, "recent") == "in_flight"


def test_pure_api_or_unresolvable_left_open(tmp_path: Path, monkeypatch) -> None:
    """A run batch-status could not batch (pure-API / unresolvable) stays open, listed."""
    upsert_run(tmp_path, _record("api", job_ids=["7"]))
    upsert_run(tmp_path, _record("mystery", job_ids=["8"]))
    _stub_batch(
        monkeypatch,
        runs={},
        skipped=[
            {"run_id": "api", "reason": "pure_api_backend"},
            {"run_id": "mystery", "reason": "unresolvable_scheduler"},
        ],
        queries=0,
    )

    out = reconcile_stale(experiment_dir=tmp_path, now=_NOW)

    assert out["closed_count"] == 0
    reasons = {e["run_id"]: e["reason"] for e in out["left_open"]}
    assert reasons == {"api": "pure_api_backend", "mystery": "unresolvable_scheduler"}
    assert _status(tmp_path, "api") == "in_flight"
    assert _status(tmp_path, "mystery") == "in_flight"


def test_unreachable_leaves_all_open(tmp_path: Path, monkeypatch) -> None:
    """An unreachable cluster (batch-status raises) closes nothing — never actuate on a blip."""
    upsert_run(tmp_path, _record("a", job_ids=["1"]))
    upsert_run(tmp_path, _record("b", job_ids=["2"]))

    def _boom(*, experiment_dir):
        raise errors.SshUnreachable("login node down")

    monkeypatch.setattr(mod, "batch_status", _boom)

    out = reconcile_stale(experiment_dir=tmp_path, now=_NOW)

    assert out["unreachable"] is True
    assert out["closed_count"] == 0
    assert out["queries"] == 0
    assert {e["run_id"] for e in out["left_open"]} == {"a", "b"}
    assert all("batch_status_unreachable" in e["reason"] for e in out["left_open"])
    assert _status(tmp_path, "a") == "in_flight"
    assert _status(tmp_path, "b") == "in_flight"


def test_mixed_known_and_unknown(tmp_path: Path, monkeypatch) -> None:
    """One run known, one unknown → exactly one closed, one open, one batch call."""
    upsert_run(tmp_path, _record("dead", job_ids=["9"]))
    upsert_run(tmp_path, _record("alive", job_ids=["7"]))
    runs = {
        "dead": {"job_states": {}, "missing_job_ids": ["9"]},
        "alive": {"job_states": {"7": "pending"}, "missing_job_ids": []},
    }
    calls = _stub_batch(monkeypatch, runs=runs, queries=1)

    out = reconcile_stale(experiment_dir=tmp_path, now=_NOW)

    assert len(calls) == 1
    assert out["closed_by_class"] == {"abandoned": 1}
    assert {c["run_id"] for c in out["closed"]} == {"dead"}
    assert out["left_open"] == [{"run_id": "alive", "reason": "scheduler_still_knows"}]
    assert _status(tmp_path, "dead") == "abandoned"
    assert _status(tmp_path, "alive") == "in_flight"


def test_bad_now_raises_spec_invalid(tmp_path: Path) -> None:
    """A non-ISO ``now`` override is rejected before any cluster touch."""
    with pytest.raises(errors.SpecInvalid):
        reconcile_stale(experiment_dir=tmp_path, now="not-a-timestamp")


def test_no_in_flight_runs_is_noop(tmp_path: Path, monkeypatch) -> None:
    """Zero in-flight runs → nothing examined, nothing closed."""
    _stub_batch(monkeypatch, runs={}, queries=0)
    out = reconcile_stale(experiment_dir=tmp_path, now=_NOW)
    assert out["examined"] == 0
    assert out["closed_count"] == 0
    assert out["left_open"] == []
