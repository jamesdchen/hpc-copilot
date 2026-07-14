"""The default aggregate path persists a durable local ``metrics_aggregate.json``.

The reproduction-receipt feature needs a byte-readable reduced-metrics artifact
for BOTH runs it compares. On the DEFAULT (non-``HPC_CLUSTER_FINAL_REDUCE``)
path, ``aggregate_flow`` used to return ``aggregated_metrics`` inline and persist
only the pulled ``_combiner/wave_*.json`` partials — no single durable file. A
``_aggregated/<run_id>/metrics_aggregate.json`` existed only on the opt-in
cluster-final-reduce path.

These tests pin that the default local reduce AND its per-task fallback now write
that durable artifact — matching the shape ``_cluster_final_reduce`` produces and
consumes (``{"aggregated_metrics": ..., "provenance": {...}}``) — while staying a
best-effort harvest-guard write (a failed write warns but never aborts) and
leaving the cluster-final path untouched.

The SSH seams (``rsync_pull``, the local reducer) are mocked exactly like the
sibling ``test_flow_ssh_default_reducer`` tests; nothing touches a cluster.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hpc_agent._wire.workflows.aggregate_flow import AggregateFlowSpec
from hpc_agent.ops import aggregate_flow as af_module
from hpc_agent.ops.aggregate_flow import aggregate_flow
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord
from hpc_agent.state.runs import write_run_sidecar

_RUN_ID = "20260623-120000-pi0"
_PI_VALUES = [3.1404, 3.1421, 3.1399, 3.1430, 3.1410]
_EXPECTED_MEAN = sum(_PI_VALUES) / len(_PI_VALUES)


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    d = tmp_path / "exp"
    d.mkdir()
    return d


def _seed_run(experiment: Path) -> RunRecord:
    record = RunRecord(
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
    )
    upsert_run(experiment, record)
    return record


def _seed_sidecar_no_reducer(experiment: Path) -> None:
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
        wave_map={},
        remote_path="/u/scratch/exp",
    )


def _rsync_combiner_ok(*_a, remote_subdir: str, local_dir: str, **_kw):
    """Every pull succeeds and creates its local dir (combiner partials present)."""
    Path(local_dir).mkdir(parents=True, exist_ok=True)
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def _combiner_missing_rsync(metrics: list[dict]):
    """``_combiner`` pull 404s; ``results`` pull writes per-task metrics.json."""

    def _stub(*_a, remote_subdir: str, local_dir: str, **_kw):
        if remote_subdir == "_combiner":
            return subprocess.CompletedProcess(
                args=[],
                returncode=23,
                stdout="",
                stderr='rsync: link_stat "_combiner" failed: No such file or directory (2)',
            )
        if remote_subdir == "results":
            dest = Path(local_dir)
            dest.mkdir(parents=True, exist_ok=True)
            for i, m in enumerate(metrics):
                td = dest / f"task-{i}"
                td.mkdir(parents=True, exist_ok=True)
                (td / "metrics.json").write_text(json.dumps(m), encoding="utf-8")
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    return _stub


def _artifact_path(experiment: Path) -> Path:
    return experiment / "_aggregated" / _RUN_ID / "metrics_aggregate.json"


def test_default_local_reduce_persists_durable_artifact(journal_home, experiment, monkeypatch):
    """Default combiner-only reduce writes metrics_aggregate.json that byte-parses
    back equal to the returned aggregated_metrics, with an honest source."""
    _seed_run(experiment)
    _seed_sidecar_no_reducer(experiment)

    fixed = {"g0": {"pi_estimate": _EXPECTED_MEAN, "n_samples": len(_PI_VALUES)}}
    monkeypatch.setattr(af_module, "rsync_pull", _rsync_combiner_ok)
    monkeypatch.setattr(af_module, "reduce_partials", lambda _dir, **_kw: fixed)
    monkeypatch.setattr(af_module, "collect_wave_errors", lambda _dir, **_kw: [3])

    result = aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_RUN_ID))

    art = _artifact_path(experiment)
    assert art.is_file()
    data = json.loads(art.read_text(encoding="utf-8"))
    # Byte-parity: the persisted aggregated_metrics equals what was returned.
    assert data["aggregated_metrics"] == result.aggregated_metrics == fixed
    prov = data["provenance"]
    assert prov["source"] == "local_reduce"
    assert prov["incomplete_waves"] == [3]  # threaded from collect_wave_errors
    assert isinstance(prov["reduced_at"], str) and prov["reduced_at"]


def test_per_task_fallback_persists_artifact_with_honest_source(
    journal_home, experiment, monkeypatch
):
    """The no-combiner per-task fallback writes the artifact too, tagged
    ``per_task_fallback`` — the source is honest about which reduce ran."""
    _seed_run(experiment)
    _seed_sidecar_no_reducer(experiment)

    metrics = [{"pi_estimate": v, "n_samples": 1} for v in _PI_VALUES]
    monkeypatch.setattr(af_module, "rsync_pull", _combiner_missing_rsync(metrics))

    result = aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_RUN_ID))

    art = _artifact_path(experiment)
    assert art.is_file()
    data = json.loads(art.read_text(encoding="utf-8"))
    assert data["aggregated_metrics"] == result.aggregated_metrics
    assert data["aggregated_metrics"][_RUN_ID]["pi_estimate"] == pytest.approx(_EXPECTED_MEAN)
    prov = data["provenance"]
    assert prov["source"] == "per_task_fallback"
    assert prov["incomplete_waves"] == []  # no wave partials → no per-wave signal


def test_write_failure_warns_but_flow_still_returns(journal_home, experiment, monkeypatch, capsys):
    """A failed persist is best-effort: it warns loudly and NEVER aborts the
    harvest — the reduced metrics are still returned inline."""
    _seed_run(experiment)
    _seed_sidecar_no_reducer(experiment)

    fixed = {"g0": {"pi_estimate": _EXPECTED_MEAN, "n_samples": len(_PI_VALUES)}}
    monkeypatch.setattr(af_module, "rsync_pull", _rsync_combiner_ok)
    monkeypatch.setattr(af_module, "reduce_partials", lambda _dir, **_kw: fixed)
    monkeypatch.setattr(af_module, "collect_wave_errors", lambda _dir, **_kw: [])

    def _boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(af_module, "atomic_write_json", _boom)

    result = aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_RUN_ID))

    # Flow still returned its result inline despite the write failure.
    assert result.aggregated_metrics == fixed
    # No durable artifact was produced (the write blew up)...
    assert not _artifact_path(experiment).is_file()
    # ...but a loud warning was surfaced.
    out = capsys.readouterr().out
    assert "WARNING" in out and "failed to persist" in out


def test_cluster_final_path_does_not_call_default_persist(journal_home, experiment, monkeypatch):
    """The opt-in cluster-final path is unchanged: it writes its OWN aggregate
    (via the pull), so the default local-persist helper must NOT fire there."""
    _seed_run(experiment)
    _seed_sidecar_no_reducer(experiment)

    monkeypatch.setenv("HPC_CLUSTER_FINAL_REDUCE", "1")
    # Stub the whole cluster-final reduce so no SSH is attempted.
    monkeypatch.setattr(
        af_module,
        "_cluster_final_reduce",
        lambda *_a, **_kw: ({"g0": {"pi_estimate": 3.14, "n_samples": 1}}, []),
    )
    monkeypatch.setattr(af_module, "rsync_pull", _rsync_combiner_ok)

    calls: list[str] = []
    real_persist = af_module._persist_local_aggregate

    def _spy(*a, **kw):
        calls.append("called")
        return real_persist(*a, **kw)

    monkeypatch.setattr(af_module, "_persist_local_aggregate", _spy)

    aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_RUN_ID))

    # The default-path persist helper is not invoked on the cluster-final branch.
    assert calls == []
