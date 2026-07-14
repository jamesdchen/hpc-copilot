"""Scope rigor at the aggregate-flow reduction seam (rigor-primitives T3).

Two behaviours, both cluster-free (the SSH/combine seams are mocked exactly
like the sibling ``test_flow_ssh_default_reducer`` tests):

* The scope GATE fires BEFORE any SSH attempt on a locked run — a lock is
  deliberate human state and no cluster work must precede the refusal.
* LOOK counts are PRIOR by construction (first reduction reports 0, a second
  distinct run's reduction reports 1) and a replay of the same run never
  double-counts. A scope-less run reports ``scope_looks=None`` and behaves
  byte-identically to a run with no scopes.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent._wire.workflows.aggregate_flow import AggregateFlowSpec
from hpc_agent.ops import aggregate_flow as af_module
from hpc_agent.ops.aggregate_flow import aggregate_flow
from hpc_agent.state import scopes
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord
from hpc_agent.state.runs import write_run_sidecar

if TYPE_CHECKING:
    pass

_SCOPE = "holdout"
_PI_VALUES = [3.1404, 3.1421, 3.1399, 3.1430, 3.1410]
_EXPECTED_MEAN = sum(_PI_VALUES) / len(_PI_VALUES)


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    d = tmp_path / "exp"
    d.mkdir()
    return d


def _seed_run(experiment: Path, run_id: str) -> None:
    upsert_run(
        experiment,
        RunRecord(
            run_id=run_id,
            profile="monte_carlo_pi",
            cluster="hoffman2",
            ssh_target="user@hoffman2.idre.ucla.edu",
            remote_path="/u/scratch/exp",
            job_name="monte_carlo_pi",
            job_ids=["12345678"],
            total_tasks=len(_PI_VALUES),
            submitted_at="2026-07-06T12:00:00+00:00",
            experiment_dir=str(experiment.resolve()),
            status="complete",
        ),
    )


def _seed_sidecar(experiment: Path, run_id: str, *, scope_tags: list[str] | None) -> None:
    write_run_sidecar(
        experiment,
        run_id=run_id,
        cmd_sha="0" * 64,
        hpc_agent_version="0.11.0",
        submitted_at="2026-07-06T12:00:00Z",
        executor="python3 src/run.py",
        result_dir_template="results/task-{task_id}",
        task_count=len(_PI_VALUES),
        tasks_py_sha="1" * 64,
        wave_map={},
        remote_path="/u/scratch/exp",
        scopes=scope_tags,
    )


def _weighted_mean_rsync():
    """rsync_pull stub: _combiner 404s, results writes the per-task metrics.json."""
    metrics = [{"pi_estimate": v, "n_samples": 1} for v in _PI_VALUES]

    def _stub(*_a, remote_subdir: str, local_dir: str, **_kw):
        if remote_subdir == "_combiner":
            return subprocess.CompletedProcess(
                args=[], returncode=23, stdout="", stderr="No such file or directory (2)"
            )
        if remote_subdir == "results":
            dest = Path(local_dir)
            for i, m in enumerate(metrics):
                td = dest / f"task-{i}"
                td.mkdir(parents=True, exist_ok=True)
                (td / "metrics.json").write_text(json.dumps(m), encoding="utf-8")
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    return _stub


def test_gate_fires_before_any_ssh_on_a_locked_run(journal_home, experiment, monkeypatch):
    """A locked scope refuses reduction BEFORE any SSH/combine work is attempted."""
    run_id = "20260706-120000-lck"
    _seed_run(experiment, run_id)
    _seed_sidecar(experiment, run_id, scope_tags=[_SCOPE])
    scopes.record_lock(experiment, _SCOPE, reason="embargo until preregistration")

    def _no_ssh(*_a, **_kw):
        raise AssertionError("rsync_pull must not run — the scope gate is pre-SSH")

    monkeypatch.setattr(af_module, "rsync_pull", _no_ssh)

    with pytest.raises(errors.ScopeLocked, match=_SCOPE):
        aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=run_id))


def test_scope_looks_counts_prior_not_self(journal_home, experiment, monkeypatch):
    """First reduction reports prior_looks=0; a second DISTINCT run reports 1."""
    monkeypatch.setattr(af_module, "rsync_pull", _weighted_mean_rsync())

    run_a = "20260706-120000-aaa"
    _seed_run(experiment, run_a)
    _seed_sidecar(experiment, run_a, scope_tags=[_SCOPE])
    res_a = aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=run_a))
    assert res_a.scope_looks is not None
    assert res_a.scope_looks[_SCOPE]["prior_looks"] == 0
    assert res_a.scope_looks[_SCOPE]["distinct_lineages"] == 0
    # Reduction stayed in code — the mean is correct.
    assert res_a.aggregated_metrics[run_a]["pi_estimate"] == pytest.approx(_EXPECTED_MEAN)

    run_b = "20260706-120000-bbb"
    _seed_run(experiment, run_b)
    _seed_sidecar(experiment, run_b, scope_tags=[_SCOPE])
    res_b = aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=run_b))
    assert res_b.scope_looks is not None
    # Run A's look is now PRIOR to run B's reduction.
    assert res_b.scope_looks[_SCOPE]["prior_looks"] == 1
    assert res_b.scope_looks[_SCOPE]["distinct_lineages"] == 1


def test_replay_does_not_double_count_look(journal_home, experiment, monkeypatch):
    """Re-reducing the SAME run_id leaves the ledger count unchanged (dedup)."""
    monkeypatch.setattr(af_module, "rsync_pull", _weighted_mean_rsync())

    run_id = "20260706-120000-rep"
    _seed_run(experiment, run_id)
    _seed_sidecar(experiment, run_id, scope_tags=[_SCOPE])

    aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=run_id))
    after_first = scopes.count_prior_looks(experiment, _SCOPE)
    assert after_first["prior_looks"] == 1

    # Replay: same run_id, same tree — record_look dedups, so the ledger holds.
    aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=run_id))
    after_replay = scopes.count_prior_looks(experiment, _SCOPE)
    assert after_replay == after_first  # unchanged: no double count


def test_scope_less_run_reports_none_and_is_byte_identical(journal_home, experiment, monkeypatch):
    """A scope-less run → scope_looks is None; no ledger is created."""
    monkeypatch.setattr(af_module, "rsync_pull", _weighted_mean_rsync())

    run_id = "20260706-120000-nos"
    _seed_run(experiment, run_id)
    _seed_sidecar(experiment, run_id, scope_tags=None)

    res = aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=run_id))
    assert res.scope_looks is None
    # Behaviour otherwise identical — the reduce still ran in code.
    assert res.aggregated_metrics[run_id]["pi_estimate"] == pytest.approx(_EXPECTED_MEAN)
    # No scopes tree materialized for a scope-less run.
    assert not (experiment / ".hpc" / "scopes").exists()
