"""Two-phase canary gate (#160) + canary-sidecar mirror (#162a).

submit-flow used to qsub the canary and the main array in one call, so a
broken dispatch sailed past the canary into the full run. With ``canary_only``
the canary goes out alone (``main_launched=False``); the worker verifies it and
re-invokes for the main array only on success. The canary's sidecar is mirrored
from the main run's so it can dispatch the SAME per-task executor.
"""

from __future__ import annotations

from unittest import mock

import pytest
from pydantic import ValidationError


def _spec(**overrides):
    from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec

    base = dict(
        profile="p",
        cluster="c",
        ssh_target="u@h",
        remote_path="/r",
        job_name="j",
        run_id="rX",
        total_tasks=4,
        backend="sge",
        script="run.sh",
        job_env={"EXECUTOR": "python3 .hpc/_hpc_dispatch.py"},
        result_dir_template="results/{run_id}/task_{task_id}",
    )
    base.update(overrides)
    return SubmitFlowSpec(**base)


@pytest.fixture
def _journal_home(tmp_path, monkeypatch):
    from hpc_agent.state import run_record

    monkeypatch.setattr(run_record, "HPC_HOMEDIR", tmp_path / "home_hpc")


def test_canary_only_requires_canary() -> None:
    with pytest.raises(ValidationError, match="canary_only"):
        _spec(canary=False, canary_only=True)


def test_mirror_canary_sidecar_copies_executor_with_task_count_1(tmp_path) -> None:
    from hpc_agent.ops.submit_flow import _mirror_canary_sidecar
    from hpc_agent.state.runs import read_run_sidecar, run_sidecar_path, write_run_sidecar

    write_run_sidecar(
        tmp_path,
        run_id="r1",
        cmd_sha="c",
        hpc_agent_version="v",
        submitted_at="2026-01-01T00:00:00+00:00",
        executor="python run.py --seed $SEED",
        result_dir_template="results/{task_id}",
        task_count=8,
        tasks_py_sha="",
    )
    assert not run_sidecar_path(tmp_path, "r1-canary").is_file()
    _mirror_canary_sidecar(tmp_path, "r1", "r1-canary")
    csc = read_run_sidecar(tmp_path, "r1-canary")
    # Same per-task command as the main run, but a single task.
    assert csc["executor"] == "python run.py --seed $SEED"
    assert csc["result_dir_template"] == "results/{task_id}"
    assert csc["task_count"] == 1


def test_mirror_canary_sidecar_noop_when_main_missing(tmp_path) -> None:
    from hpc_agent.ops.submit_flow import _mirror_canary_sidecar
    from hpc_agent.state.runs import run_sidecar_path

    _mirror_canary_sidecar(tmp_path, "rGone", "rGone-canary")
    assert not run_sidecar_path(tmp_path, "rGone-canary").is_file()


def test_canary_only_submits_canary_not_main(tmp_path, _journal_home) -> None:
    from hpc_agent.ops import submit_flow as sf
    from hpc_agent.state.runs import read_run_sidecar, run_sidecar_path, write_run_sidecar

    # Step 6d wrote the MAIN sidecar with the real per-task executor.
    write_run_sidecar(
        tmp_path,
        run_id="rX",
        cmd_sha="",
        hpc_agent_version="",
        submitted_at="2026-01-01T00:00:00+00:00",
        executor="python run.py --seed $SEED",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=4,
        tasks_py_sha="",
    )
    spec = _spec(canary=True, canary_only=True)
    with (
        mock.patch.object(
            sf, "_augment_job_env", return_value={"EXECUTOR": "python3 .hpc/_hpc_dispatch.py"}
        ),
        mock.patch.object(sf, "build_remote_backend", return_value=mock.MagicMock()),
        mock.patch.object(sf, "_make_single_array_submission", return_value=["100"]) as mk,
        mock.patch.object(sf, "submit_and_record"),
    ):
        res = sf._submit_one_spec(experiment_dir=tmp_path, spec=spec)

    # The gate: canary out, main NOT launched.
    assert res.main_launched is False
    assert res.job_ids == []
    assert res.canary_run_id == "rX-canary"
    assert res.canary_job_ids == ["100"]
    assert mk.call_count == 1  # only the canary array; the main qsub never fired

    # The canary sidecar was mirrored from the main (real executor, 1 task).
    assert run_sidecar_path(tmp_path, "rX-canary").is_file()
    csc = read_run_sidecar(tmp_path, "rX-canary")
    assert csc["task_count"] == 1
    assert csc["executor"] == "python run.py --seed $SEED"


def test_phase2_canary_false_launches_main(tmp_path, _journal_home) -> None:
    """Phase 2 (canary=false) launches the main array: main_launched=True."""
    from hpc_agent.ops import submit_flow as sf

    spec = _spec(canary=False)
    with (
        # A real dispatcher command — the #191 shape guard now refuses a bare
        # single-token EXECUTOR like "x" (proving-run-3 extension).
        mock.patch.object(
            sf, "_augment_job_env", return_value={"EXECUTOR": "python3 .hpc/_hpc_dispatch.py"}
        ),
        mock.patch.object(sf, "build_remote_backend", return_value=mock.MagicMock()),
        mock.patch.object(sf, "_make_single_array_submission", return_value=["200"]) as mk,
        mock.patch.object(sf, "submit_and_record"),
    ):
        res = sf._submit_one_spec(experiment_dir=tmp_path, spec=spec)

    assert res.main_launched is True
    assert res.job_ids == ["200"]
    assert res.canary_done is False
    assert mk.call_count == 1  # the main array


# ── MPI canary downsizing (#293 PR4) ────────────────────────────────────────


def test_mpi_canary_resources_shrinks_to_two_ranks_one_node() -> None:
    from hpc_agent._wire.workflows.submit_flow import MpiSpec, SubmitResources
    from hpc_agent.ops.submit_flow import _mpi_canary_resources

    full = SubmitResources(
        mpi=MpiSpec(ranks=128, ranks_per_node=32, threads_per_rank=4, launcher="srun"),
        walltime_sec=3600,
    )
    canary, ranks = _mpi_canary_resources(full)
    assert ranks == 2
    # ranks=2, ranks_per_node=2 → one node; threads/launcher/walltime preserved.
    assert canary.mpi.ranks == 2
    assert canary.mpi.ranks_per_node == 2
    assert canary.mpi.threads_per_rank == 4
    assert canary.mpi.launcher == "srun"
    assert canary.walltime_sec == 3600
    # The full spec is not mutated (model_copy, not in-place).
    assert full.mpi.ranks == 128


def test_mpi_canary_resources_noop_for_non_mpi() -> None:
    from hpc_agent._wire.workflows.submit_flow import SubmitResources
    from hpc_agent.ops.submit_flow import _mpi_canary_resources

    res = SubmitResources(cpus=4, walltime_sec=600)
    canary, ranks = _mpi_canary_resources(res)
    assert ranks is None
    assert canary is res  # unchanged


def test_mpi_canary_resources_handles_none() -> None:
    from hpc_agent.ops.submit_flow import _mpi_canary_resources

    assert _mpi_canary_resources(None) == (None, None)
