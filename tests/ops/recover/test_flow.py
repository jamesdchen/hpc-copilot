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
from hpc_agent.state.journal import load_run, upsert_run
from hpc_agent.state.run_record import RunRecord
from tests.conftest import make_sidecar_json

if TYPE_CHECKING:
    from pathlib import Path

PROFILE = "ml_ridge"
CLUSTER = "test_cluster"
RUN_ID = "ml_ridge_abcd1234"


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
    upsert_run(experiment, record)
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

    def test_bypass_preempt_throttle_skips_all_preempted_guard(
        self, journal_home, experiment, tmp_path, monkeypatch
    ):
        """The auto-resume composite's posture (#299): an all-preempted set is
        exactly what it WANTS to resume, so bypass_preempt_throttle=True must
        suppress the manual "back off" raise (journal-only here, no cluster)."""
        _write_clusters_yaml(tmp_path, monkeypatch)
        _seed(experiment)
        make_sidecar_json(
            experiment,
            run_id=RUN_ID,
            cluster=CLUSTER,
            profile=PROFILE,
            tasks={"1": {"preempt": "sigterm"}, "2": {"preempt": "sigterm"}},
        )
        # Without bypass this raises Preempted (see test_all_preempted_raises);
        # with bypass it proceeds to the journal-only resubmit record.
        result = resubmit_flow(
            experiment,
            RUN_ID,
            failed_task_ids=[1, 2],
            category="preempted",
            bypass_preempt_throttle=True,
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
        record = load_run(experiment, RUN_ID)
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


class TestResubmitInvalidatesCombinedWaves:
    """F06: a resubmit re-runs failed tasks that overwrite their metrics.json,
    so any wave already combined over the pre-recovery subset is stale. The
    flow must drop exactly the affected waves from ``combined_waves`` (and flag
    them in ``failed_waves`` — the forced-recombine signal combine.py reads) so
    the next aggregate pass re-runs the combiner over the recovered data."""

    def _seed_with_waves(self, experiment, tmp_path, monkeypatch):
        _write_clusters_yaml(tmp_path, monkeypatch)
        _seed(experiment, combined_waves=[0, 1, 2])
        # Re-write the sidecar carrying the wave_map: tasks 40..79 live in wave 1.
        make_sidecar_json(
            experiment,
            run_id=RUN_ID,
            cluster=CLUSTER,
            profile=PROFILE,
            wave_map={
                "0": list(range(0, 40)),
                "1": list(range(40, 80)),
                "2": list(range(80, 100)),
            },
        )

    def test_affected_wave_invalidated(self, journal_home, experiment, tmp_path, monkeypatch):
        self._seed_with_waves(experiment, tmp_path, monkeypatch)
        # Resubmit failed tasks that all belong to wave 1.
        resubmit_flow(
            experiment,
            RUN_ID,
            failed_task_ids=[45, 60, 79],
            category="system_oom",
        )
        record = load_run(experiment, RUN_ID)
        assert 1 not in record.combined_waves  # FIRE PATH: invalidated
        assert 1 in record.failed_waves
        # Unaffected waves are untouched.
        assert 0 in record.combined_waves
        assert 2 in record.combined_waves

    def test_dedup_replay_does_not_reinvalidate(
        self, journal_home, experiment, tmp_path, monkeypatch
    ):
        self._seed_with_waves(experiment, tmp_path, monkeypatch)
        kwargs = dict(failed_task_ids=[45], category="system_oom", request_id="rid-1")
        resubmit_flow(experiment, RUN_ID, **kwargs)
        # A later force-recombine put wave 1 back into combined_waves.
        rec = load_run(experiment, RUN_ID)
        rec.combined_waves = sorted({*rec.combined_waves, 1})
        rec.failed_waves = [w for w in rec.failed_waves if w != 1]
        upsert_run(experiment, rec)
        # A dedup'd replay must NOT re-invalidate the freshly recombined wave.
        second = resubmit_flow(experiment, RUN_ID, **kwargs)
        assert second.deduped is True
        record = load_run(experiment, RUN_ID)
        assert 1 in record.combined_waves
