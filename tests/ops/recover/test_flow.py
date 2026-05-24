"""Tests for ``hpc_agent.ops.recover_flow.resubmit_flow``.

Covers the macro layer composing preempted-detection and the
journal-update. The macro applies caller-supplied resource overrides
verbatim — it does no automatic right-sizing of its own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent.ops.recover_flow import (
    ResubmitFlowResult,
    resubmit_flow,
)
from hpc_agent.state import session
from hpc_agent.state.session import RunRecord, run_record
from tests.conftest import make_sidecar_json

if TYPE_CHECKING:
    from pathlib import Path

PROFILE = "ml_ridge"
CLUSTER = "test_cluster"
RUN_ID = "ml_ridge_abcd1234"


@pytest.fixture
def journal_home(tmp_path, monkeypatch):
    home = tmp_path / "home_hpc"
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", home)
    monkeypatch.setattr(session, "HPC_HOMEDIR", home)
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


def _write_clusters_yaml(tmp_path: Path, monkeypatch):
    import yaml

    cfg = {
        CLUSTER: {
            "scheduler": "slurm",
            "ssh_target": "user@cluster.example.edu",
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
    def test_all_preempted_raises_preempted(self, journal_home, experiment, tmp_path, monkeypatch):
        _write_clusters_yaml(tmp_path, monkeypatch)
        _seed(experiment)
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
        )
        assert isinstance(result, ResubmitFlowResult)


class TestOverridePassThrough:
    def test_caller_overrides_recorded_verbatim(
        self, journal_home, experiment, tmp_path, monkeypatch
    ):
        """The retry record stores exactly the overrides the caller passed."""
        _write_clusters_yaml(tmp_path, monkeypatch)
        _seed(experiment)
        resubmit_flow(
            experiment,
            RUN_ID,
            failed_task_ids=[1],
            category="system_oom",
            overrides={"mem_mb": 16_000},
        )
        record = session.load_run(experiment, RUN_ID)
        assert record.retries["1"]["overrides"]["mem_mb"] == 16_000


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
        )
        assert result.deduped is False
        assert result.request_id
        assert result.retries["3"]["attempts"] == 1
        assert result.retries["7"]["attempts"] == 1

    def test_dedupes_on_explicit_request_id(self, journal_home, experiment, tmp_path, monkeypatch):
        _write_clusters_yaml(tmp_path, monkeypatch)
        _seed(experiment)
        kwargs: dict = dict(
            failed_task_ids=[3],
            category="system_oom",
            overrides={"mem_mb": 16_000},
            request_id="explicit-rid",
        )
        first = resubmit_flow(experiment, RUN_ID, **kwargs)
        second = resubmit_flow(experiment, RUN_ID, **kwargs)
        assert first.deduped is False
        assert second.deduped is True
        assert first.request_id == second.request_id == "explicit-rid"


class TestEnvelopeShape:
    def test_to_envelope_data_has_core_keys(self, journal_home, experiment, tmp_path, monkeypatch):
        _write_clusters_yaml(tmp_path, monkeypatch)
        _seed(experiment)
        result = resubmit_flow(
            experiment,
            RUN_ID,
            failed_task_ids=[1],
            category="system_oom",
            overrides={"mem_mb": 16_000},
        )
        env = result.to_envelope_data()
        assert {"run_id", "retries", "job_ids", "request_id", "deduped"} <= env.keys()
        assert "planner" not in env
        assert "forecast_recommendation" not in env
