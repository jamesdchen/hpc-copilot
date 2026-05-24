"""Tests for the two post-aggregation gates wired into ``aggregate-flow``.

Check 1 — non-empty rows: ``spec.min_rows > 0`` runs the cluster-side
status reporter and surfaces task ids whose CSV result has too few rows.

Check 2 — expected columns + non-NaN metric: when the run sidecar's
``results`` block declares a schema, every pulled per-task result file is
verified for the declared columns + a non-NaN metric value.

SSH / rsync primitives are mocked so the tests exercise the gate wiring
without touching a network.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest import mock

import pytest

from hpc_agent._internal import session
from hpc_agent._internal.session import RunRecord, run_record
from hpc_agent._schema_models.workflows.aggregate_flow import AggregateFlowSpec
from hpc_agent.ops.aggregate import flow as af_module
from hpc_agent.ops.aggregate.flow import aggregate_flow
from hpc_agent.state.runs import write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home_hpc"
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", home)
    monkeypatch.setattr(session, "HPC_HOMEDIR", home)
    return home


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    d = tmp_path / "exp"
    d.mkdir()
    return d


def _seed_run(experiment: Path, run_id: str = "r1") -> RunRecord:
    record = RunRecord(
        run_id=run_id,
        profile="ml_ridge",
        cluster="hoffman2",
        ssh_target="user@hoffman2.idre.ucla.edu",
        remote_path="/u/scratch/exp",
        job_name="ml_ridge",
        job_ids=["12345678"],
        total_tasks=2,
        submitted_at="2026-04-26T17:00:00+00:00",
        experiment_dir=str(experiment.resolve()),
        combined_waves=[0],
    )
    session.upsert_run(experiment, record)
    return record


def _seed_sidecar(experiment: Path, run_id: str = "r1", *, results: dict | None = None) -> None:
    write_run_sidecar(
        experiment,
        run_id=run_id,
        cmd_sha="0" * 64,
        hpc_agent_version="0.2.0",
        submitted_at="2026-01-01T00:00:00Z",
        executor="python3 src/run.py",
        result_dir_template="results/{seed}",
        task_count=2,
        tasks_py_sha="1" * 64,
        wave_map={"0": [0, 1]},
        remote_path="/u/scratch/exp",
        results=results,
    )


def _ok_rsync(*_a, **_kw):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [",".join(header)]
    lines.extend(",".join(r) for r in rows)
    path.write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Check 1 — non-empty rows
# ---------------------------------------------------------------------------


def test_min_rows_zero_skips_nonempty_gate(journal_home, experiment):
    _seed_run(experiment)
    _seed_sidecar(experiment)
    spec = AggregateFlowSpec(run_id="r1", ensure_all_combined=False)

    with (
        mock.patch.object(af_module, "rsync_pull", side_effect=_ok_rsync),
        mock.patch.object(af_module, "reduce_partials", return_value={}),
        mock.patch.object(af_module, "collect_wave_errors", return_value=set()),
    ):
        result = aggregate_flow(experiment, spec=spec)

    assert result.nonempty_rows_checked is False
    assert result.nonempty_failing_task_ids == []


def test_min_rows_surfaces_failing_task_ids(journal_home, experiment):
    """A task complete at min_rows=0 but not at min_rows=N is surfaced."""
    _seed_run(experiment)
    _seed_sidecar(experiment)
    spec = AggregateFlowSpec(run_id="r1", ensure_all_combined=False, min_rows=3)

    # Lenient (min_rows=0): both tasks complete. Strict (min_rows=3): only
    # task 1 — task 2 wrote a header-only file.
    def fake_ssh_status_report(*, min_rows, **_kw):
        if min_rows == 0:
            tasks = {"1": {"status": "complete"}, "2": {"status": "complete"}}
        else:
            tasks = {"1": {"status": "complete"}, "2": {"status": "unknown"}}
        return {"tasks": tasks, "summary": {}}

    with (
        mock.patch.object(af_module, "rsync_pull", side_effect=_ok_rsync),
        mock.patch.object(af_module, "reduce_partials", return_value={}),
        mock.patch.object(af_module, "collect_wave_errors", return_value=set()),
        mock.patch(
            "hpc_agent.ops.monitor.status.ssh_status_report",
            side_effect=fake_ssh_status_report,
        ),
    ):
        result = aggregate_flow(experiment, spec=spec)

    assert result.nonempty_rows_checked is True
    assert result.nonempty_failing_task_ids == [2]
    assert result.escalation_reason is not None
    assert "empty_result_rows:tasks=2" in result.escalation_reason


def test_min_rows_all_pass_no_failing_ids(journal_home, experiment):
    _seed_run(experiment)
    _seed_sidecar(experiment)
    spec = AggregateFlowSpec(run_id="r1", ensure_all_combined=False, min_rows=1)

    def fake_ssh_status_report(*, min_rows, **_kw):
        return {"tasks": {"1": {"status": "complete"}, "2": {"status": "complete"}}}

    with (
        mock.patch.object(af_module, "rsync_pull", side_effect=_ok_rsync),
        mock.patch.object(af_module, "reduce_partials", return_value={}),
        mock.patch.object(af_module, "collect_wave_errors", return_value=set()),
        mock.patch(
            "hpc_agent.ops.monitor.status.ssh_status_report",
            side_effect=fake_ssh_status_report,
        ),
    ):
        result = aggregate_flow(experiment, spec=spec)

    assert result.nonempty_rows_checked is True
    assert result.nonempty_failing_task_ids == []


# ---------------------------------------------------------------------------
# Check 2 — expected columns + non-NaN metric
# ---------------------------------------------------------------------------


def test_columns_gate_skipped_without_schema(journal_home, experiment):
    """No `results` block on the sidecar -> columns gate is a clean no-op."""
    _seed_run(experiment)
    _seed_sidecar(experiment, results=None)
    spec = AggregateFlowSpec(
        run_id="r1",
        ensure_all_combined=False,
        pull_summaries=True,
        summary_glob="*.csv",
    )

    with (
        mock.patch.object(af_module, "rsync_pull", side_effect=_ok_rsync),
        mock.patch.object(af_module, "reduce_partials", return_value={}),
        mock.patch.object(af_module, "collect_wave_errors", return_value=set()),
    ):
        result = aggregate_flow(experiment, spec=spec)

    assert result.columns_checked is False
    assert result.column_violations == []


def test_columns_gate_flags_violations(journal_home, experiment):
    """Declared schema + a NaN metric cell in a pulled file -> violation."""
    _seed_run(experiment)
    _seed_sidecar(
        experiment,
        results={"expected_columns": ["seed", "qlike"], "metric_column": "qlike"},
    )
    spec = AggregateFlowSpec(
        run_id="r1",
        ensure_all_combined=False,
        pull_summaries=True,
        summary_glob="*.csv",
    )

    # rsync_pull is mocked; write the "pulled" summaries into the place the
    # flow expects them (output_dir / summaries).
    out = experiment / "_aggregated" / "r1"
    _write_csv(out / "summaries" / "task_1" / "out.csv", ["seed", "qlike"], [["7", "0.4"]])
    _write_csv(out / "summaries" / "task_2" / "out.csv", ["seed", "qlike"], [["8", "NaN"]])

    with (
        mock.patch.object(af_module, "rsync_pull", side_effect=_ok_rsync),
        mock.patch.object(af_module, "reduce_partials", return_value={}),
        mock.patch.object(af_module, "collect_wave_errors", return_value=set()),
    ):
        result = aggregate_flow(experiment, spec=spec)

    assert result.columns_checked is True
    assert len(result.column_violations) == 1
    assert result.column_violations[0]["metric_nan"] is True
    assert result.escalation_reason is not None
    assert "column_violations" in result.escalation_reason


def test_columns_gate_skipped_without_pulled_summaries(journal_home, experiment):
    """Schema declared but pull_summaries=False -> no local dir, gate skips."""
    _seed_run(experiment)
    _seed_sidecar(
        experiment,
        results={"expected_columns": ["seed"], "metric_column": None},
    )
    spec = AggregateFlowSpec(run_id="r1", ensure_all_combined=False, pull_summaries=False)

    with (
        mock.patch.object(af_module, "rsync_pull", side_effect=_ok_rsync),
        mock.patch.object(af_module, "reduce_partials", return_value={}),
        mock.patch.object(af_module, "collect_wave_errors", return_value=set()),
    ):
        result = aggregate_flow(experiment, spec=spec)

    assert result.columns_checked is False
    assert result.column_violations == []
