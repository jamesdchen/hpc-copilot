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

from hpc_agent._wire.workflows.aggregate_flow import AggregateFlowSpec
from hpc_agent.ops import aggregate_flow as af_module
from hpc_agent.ops.aggregate_flow import aggregate_flow
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord
from hpc_agent.state.runs import write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    d = tmp_path / "exp"
    d.mkdir()
    return d


@pytest.fixture(autouse=True)
def _force_local_reduce(monkeypatch):
    """Pin these tests to the LOCAL pull-and-reduce path they exercise.

    Rank 9 (#254) made cluster-final reduce the DEFAULT for a wave_map run; this
    module tests the local ``_combiner/`` gate wiring and mocks only ``rsync_pull``,
    so the kill switch keeps it on the local path (the cluster-final SSH is not
    mocked here). Cluster-final has its own coverage in
    ``test_flow_cluster_final_default.py``.
    """
    monkeypatch.setenv("HPC_CLUSTER_FINAL_REDUCE", "0")
    # Same reasoning for the O2 tar-pull adapter: these tests mock rsync_pull.
    monkeypatch.setenv("HPC_AGGREGATE_TAR_PULL", "0")


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
    upsert_run(experiment, record)
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
            "hpc_agent.infra.cluster_status.ssh_status_report",
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
            "hpc_agent.infra.cluster_status.ssh_status_report",
            side_effect=fake_ssh_status_report,
        ),
    ):
        result = aggregate_flow(experiment, spec=spec)

    assert result.nonempty_rows_checked is True
    assert result.nonempty_failing_task_ids == []


# --- F3: ONE reporter invocation on a current reporter -----------------------


def _single_report(tasks: dict) -> dict:
    """A current-reporter report: carries the marker + per-task rows_observed."""
    return {"rows_observed_emitted": True, "tasks": tasks, "summary": {}}


def test_min_rows_single_report_one_invocation(journal_home, experiment):
    """A reporter that emits ``rows_observed`` lets the gate derive BOTH row sets
    from ONE lenient (min_rows=0) report — exactly one ssh_status_report call."""
    _seed_run(experiment)
    _seed_sidecar(experiment)
    spec = AggregateFlowSpec(run_id="r1", ensure_all_combined=False, min_rows=3)

    calls: list[int] = []

    def fake_ssh_status_report(*, min_rows, **_kw):
        calls.append(min_rows)
        # task 1 has 5 real rows (passes), task 2 wrote a header-only CSV (0).
        return _single_report(
            {
                "1": {"status": "complete", "rows_observed": 5},
                "2": {"status": "complete", "rows_observed": 0},
            }
        )

    with (
        mock.patch.object(af_module, "rsync_pull", side_effect=_ok_rsync),
        mock.patch.object(af_module, "reduce_partials", return_value={}),
        mock.patch.object(af_module, "collect_wave_errors", return_value=set()),
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_status_report",
            side_effect=fake_ssh_status_report,
        ),
    ):
        result = aggregate_flow(experiment, spec=spec)

    assert calls == [0], "current reporter must be invoked exactly ONCE (min_rows=0)"
    assert result.nonempty_rows_checked is True
    assert result.nonempty_failing_task_ids == [2]
    assert "empty_result_rows:tasks=2" in (result.escalation_reason or "")


def test_min_rows_non_csv_complete_never_row_gated(journal_home, experiment):
    """A non-CSV complete (rows_observed None) is never demoted by the gate."""
    _seed_run(experiment)
    _seed_sidecar(experiment)
    spec = AggregateFlowSpec(run_id="r1", ensure_all_combined=False, min_rows=3)

    def fake_ssh_status_report(*, min_rows, **_kw):
        return _single_report(
            {
                "1": {"status": "complete", "rows_observed": 9},
                "2": {"status": "complete"},  # non-CSV → no rows_observed
            }
        )

    with (
        mock.patch.object(af_module, "rsync_pull", side_effect=_ok_rsync),
        mock.patch.object(af_module, "reduce_partials", return_value={}),
        mock.patch.object(af_module, "collect_wave_errors", return_value=set()),
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_status_report",
            side_effect=fake_ssh_status_report,
        ),
    ):
        result = aggregate_flow(experiment, spec=spec)

    assert result.nonempty_failing_task_ids == []


def test_min_rows_old_reporter_two_call_fallback(journal_home, experiment):
    """Version skew: a reporter that omits the ``rows_observed_emitted`` marker
    forces the historical TWO-call diff (a lenient + a strict report)."""
    _seed_run(experiment)
    _seed_sidecar(experiment)
    spec = AggregateFlowSpec(run_id="r1", ensure_all_combined=False, min_rows=3)

    calls: list[int] = []

    def fake_ssh_status_report(*, min_rows, **_kw):
        calls.append(min_rows)
        # No marker (old reporter): demotion happens reporter-side per min_rows.
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
            "hpc_agent.infra.cluster_status.ssh_status_report",
            side_effect=fake_ssh_status_report,
        ),
    ):
        result = aggregate_flow(experiment, spec=spec)

    assert calls == [0, 3], "old reporter must take the two-call (lenient+strict) fallback"
    assert result.nonempty_failing_task_ids == [2]


def test_min_rows_severed_report_is_unknown_not_all_failing(journal_home, experiment):
    """Enforcement row 10: a severed single report is UNKNOWN for BOTH row sets.
    ssh_status_report raises; the gate propagates it (UNKNOWN) rather than read a
    missing rows_observed as ``0`` → every task insufficient."""
    from hpc_agent.errors import RemoteCommandFailed

    _seed_run(experiment)
    _seed_sidecar(experiment)
    spec = AggregateFlowSpec(run_id="r1", ensure_all_combined=False, min_rows=3)

    def fake_ssh_status_report(*, min_rows, **_kw):
        raise RemoteCommandFailed(
            "status reporter channel severed / output truncated: rc 0 but no "
            "positive-evidence ack (__HPC_STATUS_ACK__)",
            returncode=0,
        )

    with (
        mock.patch.object(af_module, "rsync_pull", side_effect=_ok_rsync),
        mock.patch.object(af_module, "reduce_partials", return_value={}),
        mock.patch.object(af_module, "collect_wave_errors", return_value=set()),
        mock.patch(
            "hpc_agent.infra.cluster_status.ssh_status_report",
            side_effect=fake_ssh_status_report,
        ),
        pytest.raises(RemoteCommandFailed, match="channel severed"),
    ):
        aggregate_flow(experiment, spec=spec)


def test_nonempty_gate_docstring_is_one_invocation_not_two_ssh():
    """Pin the F3 contract at the source: the gate docstring no longer promises
    the reporter is run 'twice' / 'two SSH round-trips' unconditionally — a
    current reporter is ONE invocation, the two-call path is the skew fallback."""
    doc = af_module._nonempty_failing_task_ids.__doc__ or ""
    assert "ONE reporter invocation" in doc
    assert "two SSH round-trips" not in doc
    # The historical unconditional-twice wording is gone.
    assert "reporter twice" not in doc


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


# ---------------------------------------------------------------------------
# Regression: spec.mode is a wire-validated Literal
#
# The audit found worker_prompts/aggregate.md prescribing
# ``"mode": "auto"`` inside the spec JSON, but ``AggregateFlowSpec``
# had ``extra="forbid"`` and no ``mode`` field — every spec-driven
# invocation following the prose would have hard-failed at the schema
# boundary. ``mode`` was a function-level kwarg the CLI never wired,
# so the override paths were dead code regardless. Tests below pin
# the wire-validated field semantics.
# ---------------------------------------------------------------------------


class TestAggregateFlowSpecMode:
    @pytest.mark.parametrize("value", ["auto", "cluster-reduce", "combiner-only"])
    def test_spec_accepts_each_valid_mode(self, value: str) -> None:
        spec = AggregateFlowSpec(run_id="r1", mode=value)  # type: ignore[arg-type]
        assert spec.mode == value

    def test_spec_default_is_auto(self) -> None:
        """The 90%-case route per the worker prompt — pin the default."""
        spec = AggregateFlowSpec(run_id="r1")
        assert spec.mode == "auto"

    def test_spec_rejects_unknown_mode(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="mode"):
            AggregateFlowSpec(run_id="r1", mode="bogus")  # type: ignore[arg-type]

    def test_spec_mode_is_consumed_by_aggregate_flow(self, journal_home, experiment) -> None:
        """End-to-end: spec.mode threads into aggregate_flow without
        raising ``SpecInvalid``. The function previously rejected
        ``mode`` only via a runtime check on a kwarg the CLI never
        wired; this asserts the wire-validated field works."""
        _seed_run(experiment)
        _seed_sidecar(experiment)
        spec = AggregateFlowSpec(
            run_id="r1",
            ensure_all_combined=False,
            mode="combiner-only",  # type: ignore[arg-type]
        )

        with (
            mock.patch.object(af_module, "rsync_pull", side_effect=_ok_rsync),
            mock.patch.object(af_module, "reduce_partials", return_value={}),
            mock.patch.object(af_module, "collect_wave_errors", return_value=set()),
        ):
            result = aggregate_flow(experiment, spec=spec)

        # The flow completes (no spec_invalid) and the combiner-only
        # route produces a result.
        assert result.run_id == "r1"
