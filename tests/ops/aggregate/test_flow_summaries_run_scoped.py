"""The ``pull_summaries`` pull is run-scoped (latency audit rank 11 / finding 19 leg C).

Finding 19 (run #12) scoped the per-task fallback + trace pulls to the run's OWN
results subtree (its ``result_dir_template`` static prefix) because the scp
fallback cannot include-filter and pulling the whole shared ``results/`` root
drags every prior run's outputs through the transfer — the measured 1800s
timeout. The ``pull_summaries`` branch never received the same scoping; this test
pins that it now routes through ``_run_scoped_results_subdir`` exactly like its
sibling pulls, so a shared-root experiment with prior runs cannot re-open the
finding-19 whole-root pull on this surface.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hpc_agent._wire.workflows.aggregate_flow import AggregateFlowSpec
from hpc_agent.ops import aggregate_flow as af_module
from hpc_agent.ops.aggregate_flow import aggregate_flow
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord
from hpc_agent.state.runs import write_run_sidecar

_RUN_ID = "20260623-120000-sum"
# A real sweep template: its static prefix is ``results/causal_tune_linear`` —
# NOT the bare ``results`` root that would drag every prior run through.
_TEMPLATE = "results/causal_tune_linear/{estimator}/task-{task_id}"
_SCOPED = "results/causal_tune_linear"


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
            profile="causal_tune_linear",
            cluster="hoffman2",
            ssh_target="user@hoffman2.idre.ucla.edu",
            remote_path="/u/scratch/exp",
            job_name="causal_tune_linear",
            job_ids=["12345678"],
            total_tasks=1,
            submitted_at="2026-06-23T12:00:00+00:00",
            experiment_dir=str(experiment.resolve()),
            status="complete",
        ),
    )
    write_run_sidecar(
        experiment,
        run_id=_RUN_ID,
        cmd_sha="0" * 64,
        hpc_agent_version="0.10.0",
        submitted_at="2026-06-23T12:00:00Z",
        executor="python3 src/run.py",
        result_dir_template=_TEMPLATE,
        task_count=1,
        tasks_py_sha="1" * 64,
        wave_map={},
        remote_path="/u/scratch/exp",
    )


def test_pull_summaries_pull_is_run_scoped(journal_home, experiment, monkeypatch):
    """The summaries pull targets the run's scoped subtree, never the shared root."""
    _seed(experiment)
    seen_subdirs: list[str] = []

    def _stub(*_a, remote_subdir: str, local_dir: str, include=None, **_kw):
        seen_subdirs.append(remote_subdir)
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(af_module, "rsync_pull", _stub)

    aggregate_flow(
        experiment,
        spec=AggregateFlowSpec(
            run_id=_RUN_ID,
            pull_summaries=True,
            summary_glob="*.csv",
            mode="combiner-only",
        ),
    )

    # The summaries pull used the run-scoped prefix, NOT the bare ``results`` root.
    assert _SCOPED in seen_subdirs
    assert "results" not in seen_subdirs
