"""Tests for ``claude_hpc.orchestrator.resubmit_flow.resubmit_flow``.

Covers the macro layer composing preempted-detection, planner, advisor,
and journal-update. The constituent atoms have their own focused
tests; these check that the macro wires them in the right order with
the right data flow.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

import pytest

from claude_hpc import errors
from claude_hpc._internal import session
from claude_hpc._internal.session import RunRecord
from claude_hpc.forecast import best_submit_window as bsw
from claude_hpc.forecast import queue_wait_baseline as qwb
from claude_hpc.orchestrator.resubmit_flow import (
    ResubmitFlowResult,
    resubmit_flow,
)
from tests.conftest import make_sidecar_json, seed_diurnal_dip

if TYPE_CHECKING:
    from pathlib import Path

PROFILE = "ml_ridge"
CLUSTER = "test_cluster"
RUN_ID = "ml_ridge_abcd1234"


@pytest.fixture
def journal_home(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "HPC_HOMEDIR", tmp_path / "home_hpc")
    return tmp_path


@pytest.fixture
def experiment(tmp_path):
    d = tmp_path / "exp"
    d.mkdir()
    return d


def _seed(experiment: Path, **overrides) -> RunRecord:
    base = dict(
        run_id=RUN_ID,
        profile=PROFILE,
        cluster=CLUSTER,
        ssh_target="user@cluster.example.edu",
        remote_path="/u/scratch/exp",
        job_name=PROFILE,
        job_ids=["12345678"],
        total_tasks=100,
        submitted_at="2026-04-26T17:00:00+00:00",
        experiment_dir=str(experiment.resolve()),
    )
    base.update(overrides)
    record = RunRecord(**base)
    session.upsert_run(experiment, record)
    make_sidecar_json(
        experiment,
        run_id=RUN_ID,
        cluster=CLUSTER,
        profile=PROFILE,
    )
    return record


def _write_clusters_yaml(
    tmp_path: Path,
    monkeypatch,
    *,
    cold_start_mem_buffer: float = 0.15,
    walltime_arbitrage: bool = True,
    max_walltime_sec: int = 86400,
    max_node_mem_mb: int = 256_000,
):
    import yaml

    cfg = {
        CLUSTER: {
            "scheduler": "slurm",
            "ssh_target": "user@cluster.example.edu",
            "max_walltime_sec": max_walltime_sec,
            "cold_start_mem_buffer": cold_start_mem_buffer,
            "walltime_arbitrage": walltime_arbitrage,
            "max_node_mem_mb": max_node_mem_mb,
        }
    }
    yaml_path = tmp_path / "clusters.yaml"
    yaml_path.write_text(yaml.safe_dump(cfg))
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(yaml_path))


class TestSpecValidation:
    def test_empty_failed_task_ids_raises_spec_invalid(self, journal_home, experiment):
        _seed(experiment)
        with pytest.raises(errors.SpecInvalid):
            resubmit_flow(
                experiment,
                RUN_ID,
                failed_task_ids=[],
                category="system_oom",
            )

    def test_unknown_category_raises_spec_invalid(self, journal_home, experiment):
        _seed(experiment)
        with pytest.raises(errors.SpecInvalid):
            resubmit_flow(
                experiment,
                RUN_ID,
                failed_task_ids=[1],
                category="not_a_real_category",
            )


class TestPreemptedDetection:
    def test_all_preempted_raises_preempted(
        self, journal_home, experiment, tmp_path, monkeypatch
    ):
        _write_clusters_yaml(tmp_path, monkeypatch)
        _seed(
            experiment,
        )
        # Overwrite sidecar with a tasks block carrying preempt markers
        # for every failed id.
        make_sidecar_json(
            experiment,
            run_id=RUN_ID,
            cluster=CLUSTER,
            profile=PROFILE,
            tasks={
                "1": {"preempt": "sigterm"},
                "2": {"preempt": "sigterm"},
            },
        )
        with pytest.raises(errors.Preempted):
            resubmit_flow(
                experiment,
                RUN_ID,
                failed_task_ids=[1, 2],
                category="preempted",
                consult_forecast=False,
            )

    def test_partially_preempted_does_not_raise(
        self, journal_home, experiment, tmp_path, monkeypatch
    ):
        _write_clusters_yaml(tmp_path, monkeypatch)
        _seed(experiment)
        make_sidecar_json(
            experiment,
            run_id=RUN_ID,
            cluster=CLUSTER,
            profile=PROFILE,
            tasks={"1": {"preempt": "sigterm"}, "2": {}},
        )
        result = resubmit_flow(
            experiment,
            RUN_ID,
            failed_task_ids=[1, 2],
            category="preempted",
            consult_forecast=False,
        )
        assert isinstance(result, ResubmitFlowResult)


class TestPlannerWiring:
    def test_cold_start_grows_mem_in_planner(
        self, journal_home, experiment, tmp_path, monkeypatch
    ):
        _write_clusters_yaml(tmp_path, monkeypatch, cold_start_mem_buffer=0.15)
        _seed(experiment)
        result = resubmit_flow(
            experiment,
            RUN_ID,
            failed_task_ids=[1],
            category="system_oom",
            overrides={"mem_mb": 16_000},
            consult_forecast=False,
        )
        assert result.planner is not None
        assert result.planner.cold_start is True
        assert result.planner.overrides["mem_mb"] == 18_400

    def test_planner_overrides_flow_into_journal(
        self, journal_home, experiment, tmp_path, monkeypatch
    ):
        """The retry record stores planner-adjusted overrides, not raw caller input."""
        _write_clusters_yaml(tmp_path, monkeypatch, cold_start_mem_buffer=0.15)
        _seed(experiment)
        resubmit_flow(
            experiment,
            RUN_ID,
            failed_task_ids=[1],
            category="system_oom",
            overrides={"mem_mb": 16_000},
            consult_forecast=False,
        )
        record = session.load_run(experiment, RUN_ID)
        assert record.retries["1"]["overrides"]["mem_mb"] == 18_400

    def test_no_planner_when_cluster_unknown(
        self, journal_home, experiment, tmp_path, monkeypatch
    ):
        # Sidecar references a cluster the loaded yaml doesn't know about.
        _write_clusters_yaml(tmp_path, monkeypatch)
        _seed(experiment, cluster="ghost_cluster")
        make_sidecar_json(
            experiment,
            run_id=RUN_ID,
            cluster="ghost_cluster",
            profile=PROFILE,
        )
        result = resubmit_flow(
            experiment,
            RUN_ID,
            failed_task_ids=[1],
            category="system_oom",
            overrides={"mem_mb": 16_000},
            consult_forecast=False,
        )
        # Planner ran but found no cfg → returned overrides unmodified.
        assert result.planner is not None
        assert result.planner.overrides == {"mem_mb": 16_000}
        assert result.planner.cold_start is False

    def test_no_planner_when_sidecar_missing(
        self, journal_home, experiment, tmp_path, monkeypatch
    ):
        _write_clusters_yaml(tmp_path, monkeypatch)
        _seed(experiment)
        # Remove the sidecar after seeding.
        sidecar_path = experiment / ".hpc" / "runs" / f"{RUN_ID}.json"
        sidecar_path.unlink()
        result = resubmit_flow(
            experiment,
            RUN_ID,
            failed_task_ids=[1],
            category="system_oom",
            overrides={"mem_mb": 16_000},
            consult_forecast=False,
        )
        assert result.planner is None


class TestForecastWiring:
    def test_forecast_recommendation_attached_when_enabled(
        self, journal_home, experiment, tmp_path, monkeypatch
    ):
        _write_clusters_yaml(tmp_path, monkeypatch)
        seed_diurnal_dip(experiment, profile=PROFILE, cluster=CLUSTER)
        _seed(experiment)
        # Pin "now" to a busy hour so the dip beats it.
        fixed = datetime(2026, 4, 15, 14, 0, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(bsw, "utcnow", lambda: fixed)
        monkeypatch.setattr(qwb, "utcnow", lambda: fixed)

        result = resubmit_flow(
            experiment,
            RUN_ID,
            failed_task_ids=[1],
            category="system_oom",
            consult_forecast=True,
        )
        assert result.forecast_recommendation is not None
        assert result.forecast_recommendation.recommendation == "wait"

    def test_forecast_skipped_when_disabled(
        self, journal_home, experiment, tmp_path, monkeypatch
    ):
        _write_clusters_yaml(tmp_path, monkeypatch)
        _seed(experiment)
        result = resubmit_flow(
            experiment,
            RUN_ID,
            failed_task_ids=[1],
            category="system_oom",
            consult_forecast=False,
        )
        assert result.forecast_recommendation is None


class TestJournalUpdate:
    def test_returns_request_id_and_increments_retries(
        self, journal_home, experiment, tmp_path, monkeypatch
    ):
        _write_clusters_yaml(tmp_path, monkeypatch)
        _seed(experiment)
        result = resubmit_flow(
            experiment,
            RUN_ID,
            failed_task_ids=[3, 7],
            category="system_oom",
            overrides={"mem_mb": 16_000},
            consult_forecast=False,
        )
        assert result.deduped is False
        assert result.request_id
        assert result.retries["3"]["attempts"] == 1
        assert result.retries["7"]["attempts"] == 1

    def test_dedupes_on_explicit_request_id(
        self, journal_home, experiment, tmp_path, monkeypatch
    ):
        _write_clusters_yaml(tmp_path, monkeypatch)
        _seed(experiment)
        kwargs: dict = dict(
            failed_task_ids=[3],
            category="system_oom",
            overrides={"mem_mb": 16_000},
            request_id="explicit-rid",
            consult_forecast=False,
        )
        first = resubmit_flow(experiment, RUN_ID, **kwargs)
        second = resubmit_flow(experiment, RUN_ID, **kwargs)
        assert first.deduped is False
        assert second.deduped is True
        assert first.request_id == second.request_id == "explicit-rid"


class TestEnvelopeShape:
    def test_to_envelope_data_includes_optional_blocks(
        self, journal_home, experiment, tmp_path, monkeypatch
    ):
        _write_clusters_yaml(tmp_path, monkeypatch)
        _seed(experiment)
        result = resubmit_flow(
            experiment,
            RUN_ID,
            failed_task_ids=[1],
            category="system_oom",
            overrides={"mem_mb": 16_000},
            consult_forecast=False,
        )
        env = result.to_envelope_data()
        assert {"run_id", "retries", "job_ids", "request_id", "deduped"} <= env.keys()
        assert "planner" in env
        assert "forecast_recommendation" not in env  # consult_forecast=False

    def test_envelope_omits_planner_when_none(
        self, journal_home, experiment, tmp_path, monkeypatch
    ):
        _write_clusters_yaml(tmp_path, monkeypatch)
        _seed(experiment)
        sidecar_path = experiment / ".hpc" / "runs" / f"{RUN_ID}.json"
        sidecar_path.unlink()
        result = resubmit_flow(
            experiment,
            RUN_ID,
            failed_task_ids=[1],
            category="system_oom",
            consult_forecast=False,
        )
        env = result.to_envelope_data()
        assert "planner" not in env
