"""S-REDUCE part b: stage-refuse an absent custom reducer + ship a present one.

The run's declared reducer (sidecar ``aggregate_defaults.aggregate_cmd``, spec
§3.C.2) must be on disk under the experiment repo at submit time. If it is not,
:func:`_resolve_reducer_deploy_item` REFUSES the stage loudly — BEFORE any
transport — so the operator learns at submit, not hours later when the cluster
reduce dies "no such file" mid-harvest (the run-14 manual-scp class, §6 row S6).
When the reducer IS present it is threaded to :func:`deploy_runtime` as an
``extra_files`` deploy item so it ships content-hashed alongside the framework.

These exercise the gate helper directly (the ``test_submit_flow_pre_stage_smoke``
convention) so a broken path can't hide behind the full ssh pipeline.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from hpc_agent import errors
from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec
from hpc_agent.ops import submit_flow as sf

if TYPE_CHECKING:
    from pathlib import Path


def _spec(**over: Any) -> SubmitFlowSpec:
    base: dict[str, Any] = dict(
        profile="p",
        cluster="c",
        ssh_target="u@h",
        remote_path="/r",
        job_name="j",
        run_id="run-1",
        total_tasks=100,
        backend="slurm",
        script=".hpc/templates/cpu_array.sh",
        job_env={"EXECUTOR": "python3 .hpc/_hpc_dispatch.py", "HPC_CMD_SHA": "sha-abc"},
    )
    base.update(over)
    return SubmitFlowSpec(**base)


def _seed_sidecar(exp: Path, run_id: str, *, aggregate_cmd: str | None) -> None:
    from hpc_agent.state.runs import write_run_sidecar

    write_run_sidecar(
        exp,
        run_id=run_id,
        cmd_sha="0" * 64,
        hpc_agent_version="0.2.0",
        submitted_at="2026-01-01T00:00:00Z",
        executor="python3 src/run.py",
        result_dir_template="results/{seed}",
        task_count=4,
        tasks_py_sha="1" * 64,
        aggregate_defaults=({"aggregate_cmd": aggregate_cmd} if aggregate_cmd else None),
    )


# ── stage-refuse: absent reducer ───────────────────────────────────────────


def test_absent_reducer_refuses_stage(tmp_path: Path):
    spec = _spec()
    _seed_sidecar(tmp_path, spec.run_id, aggregate_cmd="python3 specs/reduce_missing.py")
    # No specs/reduce_missing.py on disk → loud refusal naming the reducer path.
    with pytest.raises(errors.SpecInvalid) as ei:
        sf._resolve_reducer_deploy_item(tmp_path, [spec], [0])
    msg = str(ei.value)
    assert "specs/reduce_missing.py" in msg
    assert "run-14" in msg  # the manual-scp class the gate closes


# ── present reducer → ships ─────────────────────────────────────────────────


def test_present_reducer_returns_deploy_item(tmp_path: Path):
    spec = _spec()
    reducer = tmp_path / "specs" / "reduce_ok.py"
    reducer.parent.mkdir(parents=True)
    reducer.write_text("print('reduce')\n", encoding="utf-8")
    _seed_sidecar(tmp_path, spec.run_id, aggregate_cmd="python3 specs/reduce_ok.py")

    item = sf._resolve_reducer_deploy_item(tmp_path, [spec], [0])
    assert item is not None
    reducer_abs, reducer_rel = item
    assert reducer_rel == "specs/reduce_ok.py"
    assert reducer_abs == reducer.resolve()


def test_module_reducer_no_gate_no_item(tmp_path: Path):
    # A `python -m` module reducer has no repo file — no gate, no ship.
    spec = _spec()
    _seed_sidecar(tmp_path, spec.run_id, aggregate_cmd="python -m pkg.reducer")
    assert sf._resolve_reducer_deploy_item(tmp_path, [spec], [0]) is None


def test_no_aggregate_cmd_no_gate_no_item(tmp_path: Path):
    spec = _spec()
    _seed_sidecar(tmp_path, spec.run_id, aggregate_cmd=None)
    assert sf._resolve_reducer_deploy_item(tmp_path, [spec], [0]) is None


# ── _push_and_deploy threads the reducer to deploy_runtime.extra_files ──────


def test_push_and_deploy_forwards_reducer_as_extra_files(tmp_path: Path):
    reducer_item = (tmp_path / "specs" / "reduce_ok.py", "specs/reduce_ok.py")
    with (
        patch(
            "hpc_agent.ops.submit_flow.rsync_push",
            return_value=SimpleNamespace(returncode=0, stderr=""),
        ),
        patch("hpc_agent.ops.submit_flow.deploy_runtime") as mock_deploy,
    ):
        sf._push_and_deploy(
            experiment_dir=tmp_path,
            ssh_target="u@h",
            remote_path="/r",
            rsync_excludes=None,
            scheduler="slurm",
            reducer_item=reducer_item,
        )
    assert mock_deploy.call_args.kwargs["extra_files"] == [reducer_item]


def test_push_and_deploy_without_reducer_passes_none(tmp_path: Path):
    with (
        patch(
            "hpc_agent.ops.submit_flow.rsync_push",
            return_value=SimpleNamespace(returncode=0, stderr=""),
        ),
        patch("hpc_agent.ops.submit_flow.deploy_runtime") as mock_deploy,
    ):
        sf._push_and_deploy(
            experiment_dir=tmp_path,
            ssh_target="u@h",
            remote_path="/r",
            rsync_excludes=None,
            scheduler="slurm",
        )
    assert mock_deploy.call_args.kwargs["extra_files"] is None
