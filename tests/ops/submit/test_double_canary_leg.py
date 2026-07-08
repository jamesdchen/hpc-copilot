"""``fire_second_canary`` — the extracted canary leg fires a fresh ``-canary2``.

The determinism-fingerprint double canary reuses ``submit_flow``'s canary
submission leg (``_fire_canary``) for a SECOND canary. This proves the wrapper
mirrors the main sidecar to ``<run_id>-canary2`` (so it dispatches the SAME
command) and records it — with the scheduler/backend seam mocked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec

if TYPE_CHECKING:
    from pathlib import Path


def _spec() -> SubmitFlowSpec:
    return SubmitFlowSpec(
        profile="ml",
        cluster="hoffman2",
        ssh_target="user@h",
        remote_path="/remote",
        job_name="ml",
        run_id="run-x",
        total_tasks=4,
        backend="slurm",
        script=".hpc/templates/cpu_array.sh",
        job_env={"K": "v"},
    )


def test_fire_second_canary_mirrors_sidecar_and_records(tmp_path: Path, monkeypatch) -> None:
    from hpc_agent.ops import submit_flow as sf
    from hpc_agent.state.runs import read_run_sidecar, write_run_sidecar

    # The main run's sidecar exists (Phase 1 wrote it); the second canary mirrors it.
    write_run_sidecar(
        tmp_path,
        run_id="run-x",
        cmd_sha="a" * 64,
        hpc_agent_version="0.0.0",
        submitted_at="2026-07-08T00:00:00Z",
        executor="python run.py --seed $SEED",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=4,
        tasks_py_sha="b" * 64,
        cluster="hoffman2",
        remote_path="/remote",
    )

    monkeypatch.setattr(sf, "build_remote_backend", lambda **_kw: object())
    monkeypatch.setattr(sf, "_make_single_array_submission", lambda *_a, **_k: ["777"])

    recorded: dict = {}

    def _fake_record(experiment_dir, *, spec, **_kw):
        recorded["run_id"] = spec.run_id
        recorded["job_ids"] = list(spec.job_ids)
        recorded["total_tasks"] = spec.total_tasks
        return (None, False)

    monkeypatch.setattr(sf, "submit_and_record", _fake_record)

    ids = sf.fire_second_canary(tmp_path, spec=_spec(), canary_run_id="run-x-canary2")

    assert ids == ["777"]
    # The -canary2 sidecar was mirrored from the main run's — SAME command, 1 task.
    sidecar = read_run_sidecar(tmp_path, "run-x-canary2")
    assert sidecar["executor"] == "python run.py --seed $SEED"
    assert sidecar["task_count"] == 1
    # It was recorded to the journal under the -canary2 id, not the first canary.
    assert recorded["run_id"] == "run-x-canary2"
    assert recorded["job_ids"] == ["777"]
    assert recorded["total_tasks"] == 1
