"""Tests for ``host-retarget`` (run-12 finding 23, RULING 1).

``host-retarget`` moves an IN-FLIGHT run to a different login node of the SAME
cluster by patching the record's one ``cluster`` key as a journaled decision —
``resolve_ssh_target`` then derives the new ``user@host`` at use time (no journal
surgery). These assert:

* the FIRES path — a same-scheduler/same-scratch failover patches the record's
  cluster key, journals the decision, and makes ``resolve_ssh_target`` return the
  new login node;
* the load-bearing guards — a missing run, a same-cluster no-op, an unknown
  cluster, a DIFFERENT scheduler, and a DIFFERENT scratch are each refused loudly
  (the last two routed to ``retarget-run``, which re-stages).

Idiom mirrors tests/ops/submit/test_retarget_run.py: a REAL journal + clusters.yaml
via env vars.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent import errors
from hpc_agent._wire.workflows.host_retarget import HostRetargetInput
from hpc_agent.ops.host_retarget import host_retarget

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "exp-abcd1234"

# discovery1 / discovery2: SAME scheduler + scratch, different login node (the
# failover pair). othersched / otherscratch: the two guard-tripping shapes.
_CLUSTERS_YAML = """\
discovery1:
  scheduler: sge
  host: discovery1.usc.edu
  user: me
  scratch: /scratch/me
discovery2:
  scheduler: sge
  host: discovery2.usc.edu
  user: me
  scratch: /scratch/me
othersched:
  scheduler: slurm
  host: slurm.example.edu
  user: me
  scratch: /scratch/me
otherscratch:
  scheduler: sge
  host: other.example.edu
  user: me
  scratch: /other/me
noderive:
  scheduler: sge
  scratch: /scratch/me
"""


def _setup(tmp_path: Path, monkeypatch: Any, *, cluster: str = "discovery2") -> Path:
    """Lay down clusters.yaml + a journal RunRecord on *cluster* (discovery2)."""
    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord

    clusters = tmp_path / "clusters.yaml"
    clusters.write_text(_CLUSTERS_YAML, encoding="utf-8")
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(clusters))
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))

    upsert_run(
        tmp_path,
        RunRecord(
            run_id=_RUN_ID,
            profile="exp",
            cluster=cluster,
            ssh_target="me@discovery2.usc.edu",
            remote_path="/scratch/me/exp",
            job_name="exp",
            job_ids=["42"],  # LIVE jobs — the run is in-flight; identity must not move
            total_tasks=2,
            submitted_at="2026-07-11T00:00:00+00:00",
            experiment_dir=str(tmp_path),
            status="in_flight",
            backend="sge",
            job_env={"HPC_CMD_SHA": "a" * 64},
        ),
    )
    return tmp_path


# ── the FIRES path ────────────────────────────────────────────────────────────


def test_failover_patches_cluster_key_and_journals(tmp_path: Path, monkeypatch: Any) -> None:
    """A discovery2→discovery1 failover: patches the record's cluster key, journals
    the decision, and resolve_ssh_target returns the new login node — jobs/identity
    unchanged."""
    from hpc_agent.infra.clusters import resolve_ssh_target
    from hpc_agent.state.decision_journal import read_decisions
    from hpc_agent.state.journal import load_run

    _setup(tmp_path, monkeypatch)

    res = host_retarget(
        tmp_path,
        spec=HostRetargetInput(run_id=_RUN_ID, cluster="discovery1", reason="fork-exhausted"),
    )

    assert res.stage_reached == "host_retargeted"
    assert res.old_cluster == "discovery2"
    assert res.new_cluster == "discovery1"
    assert res.old_ssh_target == "me@discovery2.usc.edu"
    assert res.new_ssh_target == "me@discovery1.usc.edu"
    assert res.decision_ts  # a provenance trail exists

    # The record's cluster key + provenance ssh_target moved; jobs/identity did not.
    rec = load_run(tmp_path, _RUN_ID)
    assert rec is not None
    assert rec.cluster == "discovery1"
    assert rec.ssh_target == "me@discovery1.usc.edu"
    assert rec.job_ids == ["42"]  # unchanged — same in-flight jobs
    assert rec.remote_path == "/scratch/me/exp"  # scratch unchanged
    assert rec.status == "in_flight"  # not a terminal transition

    # resolve_ssh_target now derives the NEW login node at use time.
    assert resolve_ssh_target(rec) == "me@discovery1.usc.edu"

    # The failover is journaled as a decision with directed provenance.
    decisions = read_decisions(tmp_path, "run", _RUN_ID)
    assert len(decisions) == 1
    prov = decisions[0]["provenance"]
    assert prov["directed"] is True
    assert prov["kind"] == "host-retarget"
    assert prov["old_cluster"] == "discovery2"
    assert prov["new_cluster"] == "discovery1"


# ── the load-bearing guards ───────────────────────────────────────────────────


def test_missing_run_is_refused(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(tmp_path / "clusters.yaml"))
    (tmp_path / "clusters.yaml").write_text(_CLUSTERS_YAML, encoding="utf-8")
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    with pytest.raises(errors.SpecInvalid) as exc:
        host_retarget(tmp_path, spec=HostRetargetInput(run_id="nope", cluster="discovery1"))
    assert "no run record" in str(exc.value)


def test_same_cluster_is_refused(tmp_path: Path, monkeypatch: Any) -> None:
    """A same-cluster host change is a clusters.yaml edit — nothing per-run to journal."""
    _setup(tmp_path, monkeypatch)
    with pytest.raises(errors.SpecInvalid) as exc:
        host_retarget(tmp_path, spec=HostRetargetInput(run_id=_RUN_ID, cluster="discovery2"))
    assert "already on cluster" in str(exc.value)


def test_unknown_cluster_is_refused(tmp_path: Path, monkeypatch: Any) -> None:
    _setup(tmp_path, monkeypatch)
    with pytest.raises(errors.ClusterUnknown) as exc:
        host_retarget(tmp_path, spec=HostRetargetInput(run_id=_RUN_ID, cluster="ghost"))
    assert "absent from clusters.yaml" in str(exc.value)


def test_no_derivable_host_is_refused(tmp_path: Path, monkeypatch: Any) -> None:
    """A cluster entry with no user/host yields no user@host — refused."""
    _setup(tmp_path, monkeypatch)
    with pytest.raises(errors.ClusterUnknown) as exc:
        host_retarget(tmp_path, spec=HostRetargetInput(run_id=_RUN_ID, cluster="noderive"))
    assert "no derivable user@host" in str(exc.value)


def test_different_scheduler_is_refused(tmp_path: Path, monkeypatch: Any) -> None:
    """A scheduler change is a cluster MOVE, not a login-node failover → retarget-run."""
    _setup(tmp_path, monkeypatch)
    with pytest.raises(errors.SpecInvalid) as exc:
        host_retarget(tmp_path, spec=HostRetargetInput(run_id=_RUN_ID, cluster="othersched"))
    assert "scheduler" in str(exc.value)
    assert "retarget-run" in str(exc.value)


def test_different_scratch_is_refused(tmp_path: Path, monkeypatch: Any) -> None:
    """A scratch change moves the result tree → retarget-run."""
    _setup(tmp_path, monkeypatch)
    with pytest.raises(errors.SpecInvalid) as exc:
        host_retarget(tmp_path, spec=HostRetargetInput(run_id=_RUN_ID, cluster="otherscratch"))
    assert "scratch" in str(exc.value)
    assert "retarget-run" in str(exc.value)
