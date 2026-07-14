"""Activation seeding for ``logs --all-failed`` (bug-sweep finding #13).

``fetch_logs(all_failed=True)`` re-polls status via the control-plane reporter
on the login node. Like ``record_status`` / ``verify_canary``, it must seed the
run's cluster env activation — otherwise the reporter runs bare login-node
python that lacks ``hpc_agent`` (rc=127 on conda clusters, the run-#7/#8 class)
and ``hpc-agent logs --all-failed`` errors out at exactly the moment a run
failed and the operator wants its evidence.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent.ops.monitor import logs_atom
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "20260606-130000-lll"


def test_all_failed_activation_seeded_from_record_cluster(
    tmp_path: Path, journal_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sibling of test_record_status_activation_seeded_from_record_cluster, on the
    ``logs --all-failed`` caller. A bare (env/cluster-less) sidecar must still
    derive conda activation from the journal record's cluster — never the bare
    login-node python "" fallthrough that lands rc=127."""
    from hpc_agent.state.runs import write_run_sidecar

    experiment = tmp_path
    rec = RunRecord(
        run_id=_RUN_ID,
        profile="p",
        cluster="hoffman2",
        backend="sge",
        ssh_target="user@host",
        remote_path="/remote",
        job_name="myjob",
        job_ids=["9001"],
        total_tasks=4,
        submitted_at="2026-06-06T13:00:00+00:00",
        experiment_dir=str(experiment.resolve()),
    )
    upsert_run(experiment, rec)
    # The BARE sidecar shape written live: no cluster, no env.
    write_run_sidecar(
        experiment,
        run_id=_RUN_ID,
        cmd_sha="",
        hpc_agent_version="",
        submitted_at="2026-01-01T00:00:00+00:00",
        executor="python run.py",
        result_dir_template="results/{task_id}",
        task_count=4,
        tasks_py_sha="",
    )
    # Hermetic clusters.yaml (the c158f797 lesson): CI's packaged placeholder has
    # a conda_source but no conda_envs, so assert against a written fixture.
    clusters = experiment / "clusters_fixture.yaml"
    clusters.write_text(
        "hoffman2:\n  host: h.example\n  user: u\n  scratch: /s\n  scheduler: sge\n"
        "  conda_source: /apps/conda/etc/profile.d/conda.sh\n  conda_envs: [hpc-pi]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(clusters))

    captured: dict[str, object] = {}

    def _fake_report(**kwargs: Any) -> dict[str, Any]:
        captured["remote_activation"] = kwargs.get("remote_activation")
        # No failed tasks → resolved_task_ids empty → fetch_task_logs is skipped
        # (zero further SSH); the activation seeding is the whole point here.
        return {"tasks": {}}

    monkeypatch.setattr(logs_atom, "_ssh_status_report", _fake_report)

    out = logs_atom.fetch_logs(experiment_dir=experiment, run_id=_RUN_ID, all_failed=True)

    # Cluster-derived activation, NOT the bare-python "" fallthrough (→ rc=127).
    assert captured["remote_activation"]
    assert "conda activate hpc-pi" in str(captured["remote_activation"])
    assert out["note"] == "no failed tasks in current status report"
