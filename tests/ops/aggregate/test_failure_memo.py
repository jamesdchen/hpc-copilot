"""Deterministic-failure memo for aggregate re-runs (latency audit rank 17).

An aggregate re-run whose (run definition, remote tree fingerprint) is
byte-identical to a prior FAILED attempt returns the cached verdict as a
needs-decision brief INSTANTLY instead of re-paying the >=1800s pull (run-12 paid
two byte-identical failures 89 and 61 min apart). The memo is journal/state-
backed, evidence-carrying (cites the prior attempt), and human-overridable
(``HPC_AGGREGATE_IGNORE_MEMO=1`` or a nudge that rewrites ``cmd_sha``). It is
inert without a provable remote tree fingerprint.

The remote fingerprint SSH is stubbed (``_remote_tree_fingerprint``) so these run
cluster-free.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest import mock

import pytest

import hpc_agent.ops.aggregate_blocks as blocks
from hpc_agent import errors
from hpc_agent._wire.workflows.aggregate_blocks import AggregateCheckSpec, AggregateRunSpec
from hpc_agent._wire.workflows.aggregate_flow import AggregateFlowSpec
from hpc_agent.ops import aggregate_flow as af_module
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord
from hpc_agent.state.runs import write_run_sidecar
from tests.ops._block_fixtures import greenlight

_RUN_ID = "20260703-120000-memo"
_FP_A = "a" * 64
_FP_B = "b" * 64


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    d = tmp_path / "exp"
    d.mkdir()
    return d


def _seed(experiment: Path) -> None:
    upsert_run(
        experiment,
        RunRecord(
            run_id=_RUN_ID,
            profile="p",
            cluster="hoffman2",
            ssh_target="user@host",
            remote_path="/remote",
            job_name="p",
            job_ids=["9001"],
            total_tasks=4,
            submitted_at="2026-07-03T12:00:00+00:00",
            experiment_dir=str(experiment.resolve()),
            status="complete",
        ),
    )
    write_run_sidecar(
        experiment,
        run_id=_RUN_ID,
        cmd_sha="0" * 64,
        hpc_agent_version="0.10.0",
        submitted_at="2026-07-03T12:00:00Z",
        executor="python3 src/run.py",
        result_dir_template="results/task-{task_id}",
        task_count=4,
        tasks_py_sha="1" * 64,
        wave_map={},
        remote_path="/remote",
    )


def _fp(value: str | None):
    """A stub for ``_remote_tree_fingerprint`` returning a fixed digest."""

    def _stub(**_kw: Any) -> str | None:
        return value

    return _stub


# ── memo helpers (af_module) ──────────────────────────────────────────────────


def test_records_and_hits_on_identical_run_and_tree(journal_home, experiment, monkeypatch):
    _seed(experiment)
    monkeypatch.delenv("HPC_AGGREGATE_IGNORE_MEMO", raising=False)
    monkeypatch.setattr(af_module, "_remote_tree_fingerprint", _fp(_FP_A))

    # No prior failure -> no hit (and zero SSH even to fingerprint: ledger absent).
    assert af_module.aggregate_failure_memo_hit(experiment, _RUN_ID) is None

    af_module.record_aggregate_failure(
        experiment, _RUN_ID, errors.RemoteCommandFailed("results pull timed out after 1800s")
    )

    hit = af_module.aggregate_failure_memo_hit(experiment, _RUN_ID)
    assert hit is not None
    assert hit["verdict"] == "failed"
    assert hit["error_code"] == "remote_command_failed"
    assert "1800s" in hit["error_message"]
    # Evidence-carrying: the prior attempt's record is cited.
    assert hit["prior_attempt"]["status"] == "complete"
    assert hit["prior_attempt"]["remote_path"] == "/remote"


def test_miss_when_remote_tree_changes(journal_home, experiment, monkeypatch):
    _seed(experiment)
    monkeypatch.delenv("HPC_AGGREGATE_IGNORE_MEMO", raising=False)

    monkeypatch.setattr(af_module, "_remote_tree_fingerprint", _fp(_FP_A))
    af_module.record_aggregate_failure(experiment, _RUN_ID, errors.RemoteCommandFailed("boom"))

    # The tree moved (tasks re-ran) -> different fingerprint -> the memo misses,
    # so a genuinely-changed run is never falsely blocked.
    monkeypatch.setattr(af_module, "_remote_tree_fingerprint", _fp(_FP_B))
    assert af_module.aggregate_failure_memo_hit(experiment, _RUN_ID) is None


def test_force_flag_ignores_memo(journal_home, experiment, monkeypatch):
    _seed(experiment)
    monkeypatch.setattr(af_module, "_remote_tree_fingerprint", _fp(_FP_A))
    af_module.record_aggregate_failure(experiment, _RUN_ID, errors.RemoteCommandFailed("boom"))

    monkeypatch.setenv("HPC_AGGREGATE_IGNORE_MEMO", "1")
    assert af_module.aggregate_failure_memo_hit(experiment, _RUN_ID) is None


def test_inert_without_provable_tree_fingerprint(journal_home, experiment, monkeypatch):
    """Network down -> no fingerprint -> the memo records nothing and never hits."""
    _seed(experiment)
    monkeypatch.delenv("HPC_AGGREGATE_IGNORE_MEMO", raising=False)
    monkeypatch.setattr(af_module, "_remote_tree_fingerprint", _fp(None))

    af_module.record_aggregate_failure(experiment, _RUN_ID, errors.RemoteCommandFailed("boom"))
    # Nothing was written (unprovable tree) ...
    assert not af_module._aggregate_memo_path(experiment, _RUN_ID).is_file()
    # ... and even a later probe cannot serve a verdict.
    assert af_module.aggregate_failure_memo_hit(experiment, _RUN_ID) is None


# ── block wiring (aggregate-check / aggregate-run) ────────────────────────────


def test_check_surfaces_deterministic_failure_memo(journal_home, experiment):
    _seed(experiment)
    memo = {
        "verdict": "failed",
        "recorded_at": "2026-07-03T13:00:00Z",
        "error_code": "remote_command_failed",
        "error_message": "results pull timed out after 1800s",
        "prior_attempt": {"status": "complete", "remote_path": "/remote"},
        "tree_fingerprint": _FP_A,
    }
    with mock.patch.object(blocks, "aggregate_failure_memo_hit", return_value=memo):
        result = blocks.aggregate_check(
            experiment, spec=AggregateCheckSpec(run_id=_RUN_ID, run_preflight=False)
        )

    assert result.needs_decision is True
    issues = result.brief["integrity_issues"]
    memo_issues = [i for i in issues if i["issue"] == "deterministic_failure_memo"]
    assert len(memo_issues) == 1
    assert memo_issues[0]["auto_masked"] is False
    assert "1800s" in memo_issues[0]["detail"]["error_message"]


def test_run_serves_cached_memo_instantly_without_pull(journal_home, experiment):
    _seed(experiment)
    greenlight(experiment, "aggregate-run", run_id=_RUN_ID)
    memo = {
        "verdict": "failed",
        "error_message": "results pull timed out after 1800s",
        "prior_attempt": {"status": "complete"},
    }
    spec = AggregateRunSpec(aggregate=AggregateFlowSpec(run_id=_RUN_ID), detach=False)

    def _flow_boom(*_a, **_kw):
        raise AssertionError("aggregate_flow must NOT run — the memo serves the cached verdict")

    with (
        mock.patch.object(blocks, "aggregate_failure_memo_hit", return_value=memo),
        mock.patch.object(blocks, "aggregate_flow", _flow_boom),
    ):
        result = blocks.aggregate_run(experiment, spec=spec)

    assert result.needs_decision is True
    assert result.stage_reached == "integrity_review"
    assert result.brief["deterministic_failure_memo"] == memo


def test_run_records_failure_on_remote_command_failed(journal_home, experiment):
    _seed(experiment)
    greenlight(experiment, "aggregate-run", run_id=_RUN_ID)
    spec = AggregateRunSpec(aggregate=AggregateFlowSpec(run_id=_RUN_ID), detach=False)
    recorded: list[tuple] = []

    def _flow_fails(*_a, **_kw):
        raise errors.RemoteCommandFailed("no readable sidecars")

    def _record(exp, run_id, exc):
        recorded.append((run_id, str(exc)))

    with (
        mock.patch.object(blocks, "aggregate_failure_memo_hit", return_value=None),
        mock.patch.object(blocks, "aggregate_flow", _flow_fails),
        mock.patch.object(blocks, "record_aggregate_failure", _record),
        pytest.raises(errors.RemoteCommandFailed, match="no readable sidecars"),
    ):
        blocks.aggregate_run(experiment, spec=spec)

    # The deterministic failure was memoized (best-effort) before the re-raise.
    assert recorded == [(_RUN_ID, "no readable sidecars")]
