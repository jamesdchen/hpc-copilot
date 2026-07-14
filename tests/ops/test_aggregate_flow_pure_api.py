"""``aggregate-flow`` Class B (#337): pure-API backends reduce LOCALLY, no rsync.

A backend whose ``requires_ssh`` capability is ``False`` has no login node and
no shared filesystem to ``rsync_pull`` a ``_combiner/`` tree from. Increment 5
adds a branch — gated entirely on the capability, never the scheduler name —
that builds the backend via the shared ``backend_for_record`` helper, calls its
``fetch_results`` hook to pull each task's ``metrics.json`` into ``task-<i>``
dirs, and reduces them with the LOCAL ``reduce_metrics`` reducer.

These tests use a FAKE registered backend (no live API / network): its
``fetch_results`` writes synthetic per-task ``metrics.json`` into the dest. The
SSH ``rsync_pull`` is booby-trapped to raise, so any rsync attempt fails the
test loudly — proving the pure-API path issues ZERO ``rsync_pull``. A final
sanity test confirms the SSH path STILL rsyncs (the branch is additive, not a
replacement).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent._wire.workflows.aggregate_flow import AggregateFlowSpec
from hpc_agent.infra import backends
from hpc_agent.infra.backends import HPCBackend
from hpc_agent.ops.aggregate_flow import aggregate_flow
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "20260622-120000-bbb"
_BACKEND_NAME = "fakepureapi"


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    d = tmp_path / "exp"
    d.mkdir()
    return d


def _record(**overrides) -> RunRecord:
    base = {
        "run_id": _RUN_ID,
        "profile": "p",
        "cluster": "c",
        "ssh_target": "user@host",
        "remote_path": "/remote",
        "job_name": "p",
        "job_ids": ["9001"],
        "total_tasks": 3,
        "submitted_at": "2026-06-22T12:00:00+00:00",
        "experiment_dir": "/tmp/exp",
        "status": "complete",
    }
    base.update(overrides)
    return RunRecord(**base)


# Per-task synthetic metrics the fake backend's ``fetch_results`` writes. The
# weighted mean (weight = ``n_samples``) of ``loss`` is the expected reduce.
_TASK_METRICS = [
    {"loss": 1.0, "n_samples": 1},
    {"loss": 3.0, "n_samples": 1},
    {"loss": 5.0, "n_samples": 2},
]


@pytest.fixture
def fake_pure_api_backend(tmp_path: Path):
    """Register a ``requires_ssh=False`` backend whose ``fetch_results`` writes
    synthetic per-task ``metrics.json`` into ``task-<i>`` dirs under the dest."""

    @backends.register(_BACKEND_NAME)
    class _FakePureApiBackend(HPCBackend):
        scheduler_name = _BACKEND_NAME
        requires_ssh = False
        log_dir = "logs"

        def __init__(self, *, remote_path: str) -> None:
            self.remote_path = remote_path

        @classmethod
        def from_build_context(cls, ctx: object) -> _FakePureApiBackend:
            return cls(remote_path=ctx.remote_path)  # type: ignore[attr-defined]

        def fetch_results(self, run_id: str, dest_dir: str) -> list[str]:
            from pathlib import Path as _P

            dest = _P(dest_dir)
            dirs: list[str] = []
            for i, metrics in enumerate(_TASK_METRICS):
                task_dir = dest / f"task-{i}"
                task_dir.mkdir(parents=True, exist_ok=True)
                (task_dir / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
                dirs.append(str(task_dir))
            return dirs

        def _build_command(self, *a: object, **k: object) -> object:
            raise NotImplementedError

    try:
        yield _FakePureApiBackend
    finally:
        backends._REGISTRY.pop(_BACKEND_NAME, None)


def _boobytrap_rsync(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any ``rsync_pull`` from aggregate_flow blow up the test."""
    import hpc_agent.ops.aggregate_flow as agg

    def _explode(*_a: object, **_k: object):
        raise AssertionError("rsync_pull must NOT run on the pure-API path")

    monkeypatch.setattr(agg, "rsync_pull", _explode)


def test_pure_api_reduces_locally_with_zero_rsync(
    journal_home, experiment, fake_pure_api_backend, monkeypatch
):
    _boobytrap_rsync(monkeypatch)
    upsert_run(experiment, _record(backend=_BACKEND_NAME))

    result = aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_RUN_ID))

    # Weighted mean of loss: (1*1 + 3*1 + 5*2) / (1 + 1 + 2) = 14 / 4 = 3.5.
    assert result.aggregated_metrics == {_RUN_ID: {"loss": 3.5, "n_samples": 4}}
    # Pure-API path skips the cluster-side summaries pull entirely.
    assert result.summaries_dir_local is None
    assert result.escalation_reason is None


def test_pure_api_fetch_results_artifacts_land_under_output_dir(
    journal_home, experiment, fake_pure_api_backend, monkeypatch
):
    _boobytrap_rsync(monkeypatch)
    upsert_run(experiment, _record(backend=_BACKEND_NAME))

    result = aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_RUN_ID))

    from pathlib import Path as _P

    out = _P(result.combiner_dir_local)
    # The fake backend wrote one metrics.json per task into the dest dir.
    assert (out / "task-0" / "metrics.json").is_file()
    assert (out / "task-2" / "metrics.json").is_file()


def test_pure_api_skips_summaries_pull_even_when_requested(
    journal_home, experiment, fake_pure_api_backend, monkeypatch
):
    # pull_summaries=true would rsync on the SSH path; the pure-API branch
    # returns before that, so the booby-trapped rsync is never reached.
    _boobytrap_rsync(monkeypatch)
    upsert_run(experiment, _record(backend=_BACKEND_NAME))

    spec = AggregateFlowSpec(run_id=_RUN_ID, pull_summaries=True, summary_glob="*.csv")
    result = aggregate_flow(experiment, spec=spec)

    assert result.aggregated_metrics == {_RUN_ID: {"loss": 3.5, "n_samples": 4}}
    assert result.summaries_dir_local is None


def test_ssh_path_still_uses_rsync_unchanged(journal_home, experiment, monkeypatch):
    """Sanity: an SSH family (``requires_ssh=True`` by default) does NOT take the
    pure-API branch — it still reaches ``rsync_pull``. Booby-trapping rsync and
    seeing it fire (via the AssertionError) proves the SSH path is untouched."""
    _boobytrap_rsync(monkeypatch)
    # An empty/default backend name is conservatively requires_ssh=True.
    upsert_run(experiment, _record(backend="slurm"))

    with pytest.raises((AssertionError, errors.HpcError)) as exc_info:
        aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_RUN_ID))
    # The pure-API branch would have returned a result; instead we hit the
    # SSH machinery. Either the booby-trapped rsync fired (AssertionError) or
    # an earlier SSH step failed first — both prove the SSH path was taken.
    assert "must NOT run on the pure-API path" in str(exc_info.value) or isinstance(
        exc_info.value, errors.HpcError
    )
