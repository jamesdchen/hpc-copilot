"""Every reduce path leaves verify-reproduction its comparator input (L2).

The reproduction receipt compares two runs via a persisted
``_aggregated/<run_id>/metrics_aggregate.json``. Before this fix ONLY the SSH
combiner-only default persisted it; a run reduced through the PURE-API path or
the CLUSTER-REDUCE path never wrote the artifact, so verify-reproduction
returned an honest-but-needless ``incomparable`` (verifier finding L2, a
coverage hole, not a correctness bug). The class fix routes all three
local-reducing paths through the ONE ``_persist_local_aggregate`` seam, and
lands the opt-in cluster-final pull at the SAME canonical flat location.

These tests pin, per reduce path, that the artifact is persisted with the right
shape at the exact path verify-reproduction reads, and that a cluster-reduced /
pure-API original + reproduction is now comparable end-to-end. The remote seams
(``rsync_pull``, ``cluster_reduce``, ``run_final_reduce``, the pure-API
``fetch_results``) are mocked exactly like the sibling aggregate tests; nothing
touches a cluster.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hpc_agent._wire.queries.verify_reproduction import VerifyReproductionSpec
from hpc_agent._wire.workflows.aggregate_flow import AggregateFlowSpec
from hpc_agent.ops import aggregate_flow as af_module
from hpc_agent.ops.aggregate_flow import aggregate_flow
from hpc_agent.ops.verify_reproduction import verify_reproduction
from hpc_agent.state import run_record
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord
from hpc_agent.state.runs import write_run_sidecar

_ORIG = "orig-run"
_REPRO = "repro-run"
_METRICS = {"gp": {"pi": 3.14159, "n_samples": 1000}}


@pytest.fixture
def journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home_hpc"
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", home)
    return home


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
            profile="p",
            cluster="hoffman2",
            ssh_target="user@host",
            remote_path="/u/scratch/exp",
            job_name="p",
            job_ids=["12345678"],
            total_tasks=1,
            submitted_at="2026-01-01T00:00:00+00:00",
            experiment_dir=str(experiment.resolve()),
            status="complete",
        ),
    )


def _seed_sidecar(
    experiment: Path,
    run_id: str,
    *,
    reproduces: str | None = None,
    aggregate_cmd: str | None = None,
) -> None:
    write_run_sidecar(
        experiment,
        run_id=run_id,
        cmd_sha="0" * 64,
        hpc_agent_version="0.11.0",
        submitted_at="2026-01-01T00:00:00Z",
        executor="python3 src/run.py",
        result_dir_template="results/task-{task_id}",
        task_count=1,
        tasks_py_sha="1" * 64,
        wave_map={},
        remote_path="/u/scratch/exp",
        reproduces=reproduces,
        aggregate_defaults=({"aggregate_cmd": aggregate_cmd} if aggregate_cmd else None),
    )


def _artifact(experiment: Path, run_id: str) -> Path:
    return experiment / "_aggregated" / run_id / "metrics_aggregate.json"


# --------------------------------------------------------------------------- #
# pure-API path
# --------------------------------------------------------------------------- #
def _run_pure_api(experiment, monkeypatch, run_id: str) -> None:
    """Drive aggregate_flow through the pure-API branch, stubbing the reduce."""
    monkeypatch.setattr(af_module, "backend_requires_ssh", lambda _b: False)
    monkeypatch.setattr(
        af_module,
        "_pure_api_reduce",
        lambda *_a, **_kw: {"gp": dict(_METRICS["gp"])},
    )
    aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=run_id))


def test_pure_api_path_persists_artifact_with_honest_source(journal_home, experiment, monkeypatch):
    _seed_run(experiment, _ORIG)
    _seed_sidecar(experiment, _ORIG)

    _run_pure_api(experiment, monkeypatch, _ORIG)

    art = _artifact(experiment, _ORIG)
    assert art.is_file()
    data = json.loads(art.read_text(encoding="utf-8"))
    assert data["aggregated_metrics"] == {"gp": _METRICS["gp"]}
    prov = data["provenance"]
    assert prov["source"] == "pure_api"
    assert prov["incomplete_waves"] == []


def test_pure_api_original_and_reproduction_are_comparable(journal_home, experiment, monkeypatch):
    """End-to-end: two pure-API runs persist artifacts, verify-reproduction matches."""
    _seed_run(experiment, _ORIG)
    _seed_run(experiment, _REPRO)
    _seed_sidecar(experiment, _ORIG)
    _seed_sidecar(experiment, _REPRO, reproduces=_ORIG)

    _run_pure_api(experiment, monkeypatch, _ORIG)
    _run_pure_api(experiment, monkeypatch, _REPRO)

    res = verify_reproduction(
        experiment, spec=VerifyReproductionSpec(original_run_id=_ORIG, repro_run_id=_REPRO)
    )
    assert res.stage_reached == "match"
    assert res.receipt["sources"]["original_artifact"].endswith("metrics_aggregate.json")
    assert res.receipt["sources"]["repro_artifact"].endswith("metrics_aggregate.json")


# --------------------------------------------------------------------------- #
# cluster-reduce path
# --------------------------------------------------------------------------- #
def _run_cluster_reduce(experiment, monkeypatch, run_id: str) -> None:
    """Drive aggregate_flow through the cluster-reduce branch, stubbing the reducer."""

    def _fake_cluster_reduce(*_a, **_kw):
        return {"ok": True, "run_id": run_id, "reduced": {"gp": dict(_METRICS["gp"])}}

    monkeypatch.setattr(
        "hpc_agent.ops.aggregate.cluster_reduce.cluster_reduce", _fake_cluster_reduce
    )
    aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=run_id, mode="cluster-reduce"))


def test_cluster_reduce_path_persists_artifact_with_honest_source(
    journal_home, experiment, monkeypatch
):
    _seed_run(experiment, _ORIG)
    _seed_sidecar(experiment, _ORIG, aggregate_cmd="python3 reducer.py")

    _run_cluster_reduce(experiment, monkeypatch, _ORIG)

    art = _artifact(experiment, _ORIG)
    assert art.is_file()
    data = json.loads(art.read_text(encoding="utf-8"))
    assert data["aggregated_metrics"] == {"gp": _METRICS["gp"]}
    assert data["provenance"]["source"] == "cluster_reduce"
    assert data["provenance"]["incomplete_waves"] == []


def test_cluster_reduce_original_and_reproduction_are_comparable(
    journal_home, experiment, monkeypatch
):
    """End-to-end: a cluster-reduced original + reproduction are comparable."""
    _seed_run(experiment, _ORIG)
    _seed_run(experiment, _REPRO)
    _seed_sidecar(experiment, _ORIG, aggregate_cmd="python3 reducer.py")
    _seed_sidecar(experiment, _REPRO, reproduces=_ORIG, aggregate_cmd="python3 reducer.py")

    _run_cluster_reduce(experiment, monkeypatch, _ORIG)
    _run_cluster_reduce(experiment, monkeypatch, _REPRO)

    res = verify_reproduction(
        experiment, spec=VerifyReproductionSpec(original_run_id=_ORIG, repro_run_id=_REPRO)
    )
    assert res.stage_reached == "match"


def test_cluster_reduce_non_dict_reducer_output_persists_empty_and_stays_incomparable(
    journal_home, experiment, monkeypatch
):
    """A reducer emitting a non-dict JSON (scalar/list) has no keyed metric shape:
    the artifact persists ``aggregated_metrics: {}`` — the HONEST equivalent — so
    the pair loads but produces no comparable keys (incomparable), never a
    fabricated scalar comparison."""
    _seed_run(experiment, _ORIG)
    _seed_run(experiment, _REPRO)
    _seed_sidecar(experiment, _ORIG, aggregate_cmd="python3 reducer.py")
    _seed_sidecar(experiment, _REPRO, reproduces=_ORIG, aggregate_cmd="python3 reducer.py")

    def _scalar_reduce(*_a, **_kw):
        return {"ok": True, "reduced": 3.14159}  # a bare scalar, not a dict

    monkeypatch.setattr("hpc_agent.ops.aggregate.cluster_reduce.cluster_reduce", _scalar_reduce)
    aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_ORIG, mode="cluster-reduce"))
    aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_REPRO, mode="cluster-reduce"))

    for rid in (_ORIG, _REPRO):
        data = json.loads(_artifact(experiment, rid).read_text(encoding="utf-8"))
        assert data["aggregated_metrics"] == {}  # honest empty, never the raw scalar

    res = verify_reproduction(
        experiment, spec=VerifyReproductionSpec(original_run_id=_ORIG, repro_run_id=_REPRO)
    )
    # Both loaded (not a missing-artifact incomparable) but produced no keys.
    assert res.stage_reached == "incomparable"
    assert "no comparable metric" in res.reason


# --------------------------------------------------------------------------- #
# cluster-final path lands at the flat canonical location (not nested)
# --------------------------------------------------------------------------- #
def test_cluster_final_pull_lands_at_flat_verify_read_location(
    journal_home, experiment, monkeypatch
):
    """HPC_CLUSTER_FINAL_REDUCE=1 pulls the cluster aggregate to the SAME flat
    ``_aggregated/<run_id>/metrics_aggregate.json`` verify-reproduction reads —
    not the ``_aggregated/<run_id>/_aggregated/<run_id>/`` nest it used to."""
    _seed_run(experiment, _ORIG)
    _seed_sidecar(experiment, _ORIG)
    monkeypatch.setenv("HPC_CLUSTER_FINAL_REDUCE", "1")

    def _fake_final_reduce(*, ssh_target, remote_path, run_id, force, remote_activation):
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    def _fake_pull(*, ssh_target, remote_path, remote_subdir, local_dir, include=None, **_kw):
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        (Path(local_dir) / "metrics_aggregate.json").write_text(
            json.dumps({"run_id": _ORIG, "aggregated_metrics": _METRICS, "provenance": {}}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr("hpc_agent.infra.transport.run_final_reduce", _fake_final_reduce)
    monkeypatch.setattr(af_module, "rsync_pull", _fake_pull)

    aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_ORIG))

    flat = _artifact(experiment, _ORIG)
    nested = experiment / "_aggregated" / _ORIG / "_aggregated" / _ORIG / "metrics_aggregate.json"
    assert flat.is_file()
    assert not nested.exists()
    assert json.loads(flat.read_text(encoding="utf-8"))["aggregated_metrics"] == _METRICS
