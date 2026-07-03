"""FIX-1: the loud post-greenlight drift guard on the S3 main-array launch.

``launch_main_array`` (the S3 seam) skips rsync+deploy — Phase 1 already shipped
the tree the S2 canary verified. A local edit to ``.hpc/tasks.py`` AFTER the
greenlight would otherwise launch the full array on code the canary never saw.
:func:`_assert_no_post_greenlight_drift` closes that gap: it compares the
sidecar's canary-time ``tasks_py_sha`` baseline against a fresh compute of the
on-disk ``.hpc/tasks.py`` (routed through the one drift predicate) and raises
``SpecInvalid`` on drift. A missing baseline cannot prove drift → it launches.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent._wire.workflows.submit_and_verify import SubmitAndVerifySpec
from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec
from hpc_agent.ops import submit_and_verify as sav
from hpc_agent.state.runs import run_sidecar_path

if TYPE_CHECKING:
    from pathlib import Path

RUN_ID = "ml_run_abcd1234"


def _spec() -> SubmitAndVerifySpec:
    return SubmitAndVerifySpec(
        submit=SubmitFlowSpec(
            profile="ml",
            cluster="hoffman2",
            ssh_target="user@hoffman2.idre.ucla.edu",
            remote_path="/u/scratch/exp",
            job_name="ml",
            run_id=RUN_ID,
            total_tasks=4,
            backend="slurm",
            script=".hpc/templates/cpu_array.sh",
            job_env={"EXECUTOR": "python train.py"},
        ),
        poll_interval_sec=1,
        wait_budget_sec=5,
    )


def _write_tasks_py(exp: Path, body: bytes = b"print('v1')\n") -> str:
    tp = exp / ".hpc" / "tasks.py"
    tp.parent.mkdir(parents=True, exist_ok=True)
    tp.write_bytes(body)
    return hashlib.sha256(body).hexdigest()


def _write_sidecar(exp: Path, *, tasks_py_sha: str, executor: str = "python train.py") -> None:
    path = run_sidecar_path(exp, RUN_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"executor": executor, "tasks_py_sha": tasks_py_sha, "task_count": 4}),
        encoding="utf-8",
    )


@pytest.fixture
def stub_launch(monkeypatch: pytest.MonkeyPatch) -> list:
    """Replace the real Phase-2 submit with a stub — no SSH/scheduler."""
    calls: list = []

    def _fake(experiment_dir, base):  # type: ignore[no-untyped-def]
        calls.append((experiment_dir, base))
        from hpc_agent.ops.submit_flow import SubmitFlowResult

        return SubmitFlowResult(
            run_id=RUN_ID,
            job_ids=["999"],
            total_tasks=4,
            deduped=False,
            canary_done=False,
            canary_run_id=None,
            canary_job_ids=None,
        )

    monkeypatch.setattr(sav, "_launch_main_array", _fake)
    return calls


def test_drifted_tasks_py_raises_and_does_not_launch(tmp_path: Path, stub_launch: list) -> None:
    _write_tasks_py(tmp_path)  # current bytes
    _write_sidecar(tmp_path, tasks_py_sha="deadbeef" * 8)  # baseline differs → drift
    with pytest.raises(errors.SpecInvalid, match="drifted since the canary greenlight"):
        sav.launch_main_array(tmp_path, spec=_spec())
    assert stub_launch == []  # the main array never launched


def test_unchanged_tree_launches(tmp_path: Path, stub_launch: list) -> None:
    sha = _write_tasks_py(tmp_path)
    _write_sidecar(tmp_path, tasks_py_sha=sha)  # baseline == current → no drift
    res = sav.launch_main_array(tmp_path, spec=_spec())
    assert res.verified is True
    assert len(stub_launch) == 1


def test_missing_sidecar_baseline_launches(tmp_path: Path, stub_launch: list) -> None:
    _write_tasks_py(tmp_path)
    # No sidecar written → no canary-time baseline → cannot prove drift → launch.
    res = sav.launch_main_array(tmp_path, spec=_spec())
    assert res.verified is True
    assert len(stub_launch) == 1
