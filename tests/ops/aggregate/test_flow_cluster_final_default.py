"""Cluster-final reduce is the DEFAULT (latency audit rank 9 / #254).

The #254 cross-wave reduce runs ON THE CLUSTER and pulls a single KB
``metrics_aggregate.json`` instead of every ``wave_*.json`` partial (re-paid on
every re-aggregate). Rank 9 flips it from opt-in to the default:

* env unset (default) -> cluster-final; ``reduce_path == "cluster_final"``.
* cluster-final FAILS by default -> automatic fallback to the local
  pull-and-reduce (the aggregate is still produced), ``reduce_path`` names the
  local engine, and the downgrade is disclosed.
* ``HPC_CLUSTER_FINAL_REDUCE=0`` -> the kill switch forces the local path; the
  cluster reduce is NEVER invoked.
* ``HPC_CLUSTER_FINAL_REDUCE=1`` -> legacy strict opt-in: a cluster-final
  failure RAISES (no silent downgrade).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent._wire.workflows.aggregate_flow import AggregateFlowSpec
from hpc_agent.ops import aggregate_flow as af_module
from hpc_agent.ops.aggregate_flow import aggregate_flow
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord
from hpc_agent.state.runs import write_run_sidecar

_RUN_ID = "20260623-120000-cf0"
_PI_VALUES = [3.14, 3.15, 3.16]


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    d = tmp_path / "exp"
    d.mkdir()
    return d


def _seed(experiment: Path, *, wave_map: dict | None = None) -> None:
    """Seed a terminal run. A non-empty *wave_map* signals 'combiner deployed'
    (the default cluster-final gate); the empty default is the no-combiner
    ``@register_run`` sweep shape."""
    upsert_run(
        experiment,
        RunRecord(
            run_id=_RUN_ID,
            profile="monte_carlo_pi",
            cluster="hoffman2",
            ssh_target="user@hoffman2.idre.ucla.edu",
            remote_path="/u/scratch/exp",
            job_name="monte_carlo_pi",
            job_ids=["12345678"],
            total_tasks=len(_PI_VALUES),
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
        result_dir_template="results/task-{task_id}",
        task_count=len(_PI_VALUES),
        tasks_py_sha="1" * 64,
        wave_map=wave_map or {},
        remote_path="/u/scratch/exp",
    )


# A one-wave map marks the run as "combiner deployed" for the default gate.
_DEPLOYED = {"0": [0, 1, 2]}


def _cluster_final_ok(monkeypatch, *, metrics: dict) -> dict:
    """Wire run_final_reduce + rsync_pull so cluster-final SUCCEEDS. Returns a
    calls-record dict the test asserts against."""
    final_calls: list[int] = []
    pulls: list[str] = []
    calls: dict[str, object] = {"final": final_calls, "pulls": pulls}

    def _final(*, ssh_target, remote_path, run_id, force, remote_activation):
        final_calls.append(1)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    def _pull(*_a, remote_subdir: str, local_dir: str, include=None, **_kw):
        pulls.append(remote_subdir)
        dest = Path(local_dir)
        dest.mkdir(parents=True, exist_ok=True)
        if remote_subdir.startswith("_aggregated"):
            (dest / "metrics_aggregate.json").write_text(
                json.dumps(
                    {
                        "run_id": _RUN_ID,
                        "aggregated_metrics": metrics,
                        "provenance": {"incomplete_waves": []},
                    }
                ),
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr("hpc_agent.infra.transport.run_final_reduce", _final)
    monkeypatch.setattr(af_module, "rsync_pull", _pull)
    return calls


def _combiner_missing_local_pull(metrics: list[dict]):
    """rsync_pull stub for the LOCAL fallback: _combiner 404s, results writes sidecars."""

    def _stub(*_a, remote_subdir: str, local_dir: str, include=None, **_kw):
        dest = Path(local_dir)
        dest.mkdir(parents=True, exist_ok=True)
        if remote_subdir.startswith("results"):
            for i, m in enumerate(metrics):
                td = dest / f"task-{i}"
                td.mkdir(parents=True, exist_ok=True)
                (td / "metrics.json").write_text(json.dumps(m), encoding="utf-8")
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        # _combiner and anything else: 404 / benign.
        if remote_subdir == "_combiner":
            return subprocess.CompletedProcess(
                args=[], returncode=23, stdout="", stderr="No such file or directory (2)"
            )
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    return _stub


def test_default_uses_cluster_final_reduce(journal_home, experiment, monkeypatch):
    """Env UNSET: cluster-final runs, only the aggregate is pulled, reduce_path discloses it."""
    monkeypatch.delenv("HPC_CLUSTER_FINAL_REDUCE", raising=False)
    _seed(experiment, wave_map=_DEPLOYED)
    calls = _cluster_final_ok(monkeypatch, metrics={"a": {"acc": 0.86, "n_samples": 3}})

    result = aggregate_flow(
        experiment, spec=AggregateFlowSpec(run_id=_RUN_ID, ensure_all_combined=False)
    )

    assert result.reduce_path == "cluster_final"
    assert result.aggregated_metrics == {"a": {"acc": 0.86, "n_samples": 3}}
    assert calls["final"] == [1]  # run_final_reduce invoked exactly once
    # ONLY the single-aggregate pull, never the _combiner wave tree.
    assert calls["pulls"] == ["_aggregated/" + _RUN_ID]


def test_cluster_final_failure_falls_back_to_local(journal_home, experiment, monkeypatch, capsys):
    """Default: cluster-final failure downgrades to the local reduce, disclosed."""
    monkeypatch.delenv("HPC_CLUSTER_FINAL_REDUCE", raising=False)
    _seed(experiment, wave_map=_DEPLOYED)

    def _final_fails(*, ssh_target, remote_path, run_id, force, remote_activation):
        return subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="python3: bad interpreter"
        )

    monkeypatch.setattr("hpc_agent.infra.transport.run_final_reduce", _final_fails)
    metrics = [{"pi_estimate": v, "n_samples": 1} for v in _PI_VALUES]
    monkeypatch.setattr(af_module, "rsync_pull", _combiner_missing_local_pull(metrics))

    result = aggregate_flow(
        experiment, spec=AggregateFlowSpec(run_id=_RUN_ID, ensure_all_combined=False)
    )

    # The local fallback produced the aggregate; reduce_path names the local engine.
    assert result.reduce_path == "per_task_fallback"
    assert result.aggregated_metrics[_RUN_ID]["pi_estimate"] == pytest.approx(
        sum(_PI_VALUES) / len(_PI_VALUES)
    )
    # The downgrade is disclosed on stdout (never a silent skip).
    assert "cluster-final reduce unavailable" in capsys.readouterr().out


def test_no_combiner_run_skips_cluster_final(journal_home, experiment, monkeypatch):
    """A no-combiner sweep (empty wave_map) never pays a cluster-final round-trip
    under the default — it has no wave partials to reduce cluster-side."""
    monkeypatch.delenv("HPC_CLUSTER_FINAL_REDUCE", raising=False)
    _seed(experiment)  # empty wave_map — the @register_run no-combiner shape

    def _final_boom(**_kw):
        raise AssertionError("cluster-final must NOT run for a no-combiner (wave_map-less) run")

    monkeypatch.setattr("hpc_agent.infra.transport.run_final_reduce", _final_boom)
    metrics = [{"pi_estimate": v, "n_samples": 1} for v in _PI_VALUES]
    monkeypatch.setattr(af_module, "rsync_pull", _combiner_missing_local_pull(metrics))

    result = aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_RUN_ID))

    assert result.reduce_path == "per_task_fallback"


def test_kill_switch_forces_local_reduce(journal_home, experiment, monkeypatch):
    """HPC_CLUSTER_FINAL_REDUCE=0 forces the local path; run_final_reduce is NEVER called."""
    monkeypatch.setenv("HPC_CLUSTER_FINAL_REDUCE", "0")
    _seed(experiment)

    def _final_boom(**_kw):
        raise AssertionError("run_final_reduce must NOT run under the kill switch")

    monkeypatch.setattr("hpc_agent.infra.transport.run_final_reduce", _final_boom)
    metrics = [{"pi_estimate": v, "n_samples": 1} for v in _PI_VALUES]
    monkeypatch.setattr(af_module, "rsync_pull", _combiner_missing_local_pull(metrics))

    result = aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_RUN_ID))

    assert result.reduce_path == "per_task_fallback"


def test_strict_opt_in_raises_on_cluster_final_failure(journal_home, experiment, monkeypatch):
    """HPC_CLUSTER_FINAL_REDUCE=1 keeps the strict contract: a failure RAISES, no fallback."""
    monkeypatch.setenv("HPC_CLUSTER_FINAL_REDUCE", "1")
    _seed(experiment)

    def _final_fails(*, ssh_target, remote_path, run_id, force, remote_activation):
        return subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="cluster reduce boom"
        )

    def _pull_boom(*_a, **_kw):
        raise AssertionError("strict opt-in must NOT fall back to the local pull")

    monkeypatch.setattr("hpc_agent.infra.transport.run_final_reduce", _final_fails)
    monkeypatch.setattr(af_module, "rsync_pull", _pull_boom)

    with pytest.raises(errors.RemoteCommandFailed, match="cluster final-reduce"):
        aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_RUN_ID))
