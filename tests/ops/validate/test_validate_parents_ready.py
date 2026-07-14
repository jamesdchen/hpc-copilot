"""Tests for the validate-parents-ready atom.

The readiness piece of the DAG kernel: every declared parent must have a
local sidecar AND a journal record at terminal-success before a child
that consumes its outputs is submitted. One finding per not-ready
parent; ``parent_states`` reports the whole frontier.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hpc_agent._wire.validators.validate_parents_ready import ValidateParentsReadySpec
from hpc_agent.ops.validate.parents_ready import validate_parents_ready
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord


def _seed_run_sidecar(experiment_dir: Path, *, run_id: str) -> None:
    runs_dir = experiment_dir / ".hpc" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "sidecar_schema_version": 2,
        "run_id": run_id,
        "cmd_sha": "a" * 64,
        "hpc_agent_version": "0.0.0-test",
        "submitted_at": "2026-06-10T00:00:00Z",
        "executor": "python3 src/test.py",
        "result_dir_template": "results/{run_id}/task_{task_id}",
        "task_count": 2,
        "tasks_py_sha": "",
        "wave_map": {"0": [0, 1]},
    }
    (runs_dir / f"{run_id}.json").write_text(json.dumps(payload, sort_keys=True))


def _seed_journal_run(experiment_dir: Path, *, run_id: str, status: str) -> None:
    record = RunRecord(
        run_id=run_id,
        profile="ml",
        cluster="hoffman2",
        ssh_target="user@hoffman2",
        remote_path="/scratch/exp",
        job_name="ml",
        job_ids=["12345"],
        total_tasks=2,
        submitted_at="2026-06-10T00:00:00Z",
        experiment_dir=str(experiment_dir),
        status=status,
    )
    upsert_run(experiment_dir, record)


def _ready(experiment_dir: Path, *, run_id: str) -> None:
    _seed_run_sidecar(experiment_dir, run_id=run_id)
    _seed_journal_run(experiment_dir, run_id=run_id, status="complete")


class TestValidateParentsReady:
    def test_all_parents_complete_passes(self, journal_home, tmp_path):
        _ready(tmp_path, run_id="p1")
        _ready(tmp_path, run_id="p2")
        result = validate_parents_ready(
            tmp_path, spec=ValidateParentsReadySpec(parent_run_ids=["p1", "p2"])
        )
        assert result.findings == []
        assert result.parent_states == {"p1": "complete", "p2": "complete"}

    def test_missing_sidecar_fires_parent_run_missing(self, journal_home, tmp_path):
        result = validate_parents_ready(
            tmp_path, spec=ValidateParentsReadySpec(parent_run_ids=["ghost"])
        )
        assert [f.code for f in result.findings] == ["parent_run_missing"]
        assert result.parent_states == {"ghost": "missing"}

    def test_sidecar_without_journal_is_unknown_not_ready(self, journal_home, tmp_path):
        """No journal record cannot prove terminal — in flight on another
        machine and a wiped journal look identical from here."""
        _seed_run_sidecar(tmp_path, run_id="p1")
        result = validate_parents_ready(
            tmp_path, spec=ValidateParentsReadySpec(parent_run_ids=["p1"])
        )
        assert [f.code for f in result.findings] == ["parent_not_terminal"]
        assert result.parent_states == {"p1": "unknown"}

    def test_in_flight_fires_parent_not_terminal(self, journal_home, tmp_path):
        _seed_run_sidecar(tmp_path, run_id="p1")
        _seed_journal_run(tmp_path, run_id="p1", status="in_flight")
        result = validate_parents_ready(
            tmp_path, spec=ValidateParentsReadySpec(parent_run_ids=["p1"])
        )
        assert [f.code for f in result.findings] == ["parent_not_terminal"]
        assert result.parent_states == {"p1": "in_flight"}

    @pytest.mark.parametrize("status", ["failed", "abandoned"])
    def test_terminal_failure_fires_parent_failed(self, journal_home, tmp_path, status):
        _seed_run_sidecar(tmp_path, run_id="p1")
        _seed_journal_run(tmp_path, run_id="p1", status=status)
        result = validate_parents_ready(
            tmp_path, spec=ValidateParentsReadySpec(parent_run_ids=["p1"])
        )
        assert [f.code for f in result.findings] == ["parent_failed"]
        assert result.parent_states == {"p1": status}

    def test_mixed_frontier_reports_every_state(self, journal_home, tmp_path):
        _ready(tmp_path, run_id="done")
        _seed_run_sidecar(tmp_path, run_id="running")
        _seed_journal_run(tmp_path, run_id="running", status="in_flight")
        result = validate_parents_ready(
            tmp_path,
            spec=ValidateParentsReadySpec(parent_run_ids=["done", "running", "ghost"]),
        )
        assert sorted(f.code for f in result.findings) == [
            "parent_not_terminal",
            "parent_run_missing",
        ]
        assert result.parent_states == {
            "done": "complete",
            "running": "in_flight",
            "ghost": "missing",
        }

    def test_duplicate_parents_checked_once(self, journal_home, tmp_path):
        result = validate_parents_ready(
            tmp_path, spec=ValidateParentsReadySpec(parent_run_ids=["ghost", "ghost"])
        )
        assert len(result.findings) == 1
        assert list(result.parent_states) == ["ghost"]
