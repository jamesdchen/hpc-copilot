"""Canary-calibrated array walltime (run-14).

The two-phase canary MEASURES a full real task before the main array launches;
these tests pin that the measurement shrinks the array walltime (shrink-only,
never above the approved ceiling), that the shrink is DISCLOSED, and that the S2
consent footprint (``est_core_hours``) recomputes off the calibrated walltime —
the run ``causal_tune_tree_lgbm-7905102a`` failure (6h ceiling / 10-min task /
36× est inflation) turned into a regression guard.

Cluster-free: the pure kernel is exercised directly; the flow seam
(``_calibrated_base``) and the S2/S3 briefs read a stamped canary sidecar written
via the real ``stamp_canary_runtime`` path — no SSH, no scheduler.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

import hpc_agent.ops.submit_blocks as blocks
from hpc_agent._wire.workflows.submit_and_verify import (
    SubmitAndVerifyResult,
    SubmitAndVerifySpec,
)
from hpc_agent._wire.workflows.submit_blocks import SubmitS2Spec
from hpc_agent._wire.workflows.submit_flow import MpiSpec, SubmitFlowSpec, SubmitResources
from hpc_agent.ops.submit.canary_calibration import (
    DEFAULT_FLOOR_SEC,
    DEFAULT_SAFETY_FACTOR,
    calibrate_array_walltime,
)
from hpc_agent.state.runs import (
    read_canary_elapsed_sec,
    stamp_canary_runtime,
    write_run_sidecar,
)
from tests.ops._block_fixtures import greenlight

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "tune_run_abcd1234"
_CANARY_ID = f"{_RUN_ID}-canary"


# ── the pure kernel ──────────────────────────────────────────────────────────


def test_shrink_applies_floor_dominates() -> None:
    """A short canary shrinks a fat cold-start ceiling down to the floor."""
    c = calibrate_array_walltime(canary_elapsed_sec=600, requested_walltime_sec=21600)
    assert c.applied is True
    # 3× 600 = 1800 == floor; min(1800, 21600) = 1800.
    assert c.walltime_sec == 1800
    assert c.canary_elapsed_sec == 600
    assert c.safety_factor == DEFAULT_SAFETY_FACTOR
    assert c.floor_sec == DEFAULT_FLOOR_SEC
    assert c.disclosure is not None
    assert "21600s → 1800s" in c.disclosure
    assert "never above the approved 21600s ceiling" in c.disclosure


def test_shrink_applies_factor_dominates() -> None:
    """When 3× the canary exceeds the floor, the factor sets the walltime."""
    c = calibrate_array_walltime(canary_elapsed_sec=1000, requested_walltime_sec=21600)
    assert c.applied is True
    assert c.walltime_sec == 3000  # max(1800, ceil(1000*3))


def test_never_exceeds_the_approved_ceiling() -> None:
    """Shrink-only: a slow canary can NEVER lift the ask above what was approved."""
    c = calibrate_array_walltime(canary_elapsed_sec=10000, requested_walltime_sec=3600)
    # 3× 10000 = 30000, clamped down to the 3600 approved ceiling — not applied.
    assert c.walltime_sec == 3600
    assert c.applied is False
    assert c.disclosure is not None
    assert "held at the approved 3600s" in c.disclosure


def test_floor_never_lifts_above_the_approved_ceiling() -> None:
    """Even the 30-min floor cannot exceed a smaller approved walltime."""
    c = calibrate_array_walltime(canary_elapsed_sec=10, requested_walltime_sec=600)
    assert c.walltime_sec == 600  # min(1800 floor, 600 ceiling)
    assert c.applied is False


def test_no_measurement_is_a_noop() -> None:
    """No canary runtime (cache-skip) → the request is carried through, no disclosure."""
    c = calibrate_array_walltime(canary_elapsed_sec=None, requested_walltime_sec=21600)
    assert c.applied is False
    assert c.walltime_sec == 21600
    assert c.disclosure is None


def test_no_requested_walltime_is_a_noop() -> None:
    """No approved ceiling → nothing to shrink; walltime stays None, no disclosure."""
    c = calibrate_array_walltime(canary_elapsed_sec=600, requested_walltime_sec=None)
    assert c.applied is False
    assert c.walltime_sec is None
    assert c.disclosure is None


def test_bad_safety_factor_falls_back_to_default() -> None:
    """A fat-fingered non-positive factor must never loosen the ceiling."""
    c = calibrate_array_walltime(
        canary_elapsed_sec=1000, requested_walltime_sec=21600, safety_factor=0
    )
    assert c.safety_factor == DEFAULT_SAFETY_FACTOR
    assert c.walltime_sec == 3000


# ── the durable stamp round-trip ─────────────────────────────────────────────


def _write_canary_sidecar(experiment_dir: Path, run_id: str = _CANARY_ID) -> None:
    write_run_sidecar(
        experiment_dir,
        run_id=run_id,
        cmd_sha="deadbeef",
        hpc_agent_version="0.0.0-test",
        submitted_at="2026-01-01T00:00:00+00:00",
        executor="python run.py",
        result_dir_template="results/{task_id}",
        task_count=1,
        tasks_py_sha="",
    )


def test_stamp_and_read_round_trip(tmp_path: Path) -> None:
    _write_canary_sidecar(tmp_path)
    assert read_canary_elapsed_sec(tmp_path, _CANARY_ID) is None  # not stamped yet
    stamp_canary_runtime(tmp_path, _CANARY_ID, elapsed_sec=612)
    assert read_canary_elapsed_sec(tmp_path, _CANARY_ID) == 612


def test_stamp_noop_on_absent_sidecar(tmp_path: Path) -> None:
    """Best-effort: no sidecar → no stamp, no raise (calibration just won't run)."""
    assert stamp_canary_runtime(tmp_path, "no-such-run", elapsed_sec=100) is None
    assert read_canary_elapsed_sec(tmp_path, "no-such-run") is None


def test_stamp_rejects_nonpositive(tmp_path: Path) -> None:
    _write_canary_sidecar(tmp_path)
    assert stamp_canary_runtime(tmp_path, _CANARY_ID, elapsed_sec=0) is None
    assert read_canary_elapsed_sec(tmp_path, _CANARY_ID) is None


# ── verify-canary surfaces the measured elapsed ──────────────────────────────


def test_parse_runtime_json_surfaces_elapsed() -> None:
    from hpc_agent.ops.verify_canary import _parse_runtime_json

    got = _parse_runtime_json('{"exit_code": 0, "elapsed_sec": 573}')
    assert got == {"status": "present", "exit_code": 0, "elapsed_sec": 573}


def test_parse_runtime_json_elapsed_absent_or_bad_is_none() -> None:
    from hpc_agent.ops.verify_canary import _parse_runtime_json

    assert _parse_runtime_json('{"exit_code": 0}')["elapsed_sec"] is None
    assert _parse_runtime_json('{"exit_code": 0, "elapsed_sec": -5}')["elapsed_sec"] is None
    assert _parse_runtime_json('{"exit_code": 0, "elapsed_sec": "x"}')["elapsed_sec"] is None


# ── the launch seam applies the shrink ───────────────────────────────────────


def _base_spec(
    *, walltime_sec: int | None = 21600, mpi: MpiSpec | None = None, total_tasks: int = 900
) -> SubmitFlowSpec:
    return SubmitFlowSpec(
        profile="tune",
        cluster="hoffman2",
        ssh_target="user@hoffman2.idre.ucla.edu",
        remote_path="/u/scratch/exp",
        job_name="tune",
        run_id=_RUN_ID,
        total_tasks=total_tasks,
        backend="slurm",
        script=".hpc/templates/cpu_array.sh",
        job_env={"K": "v"},
        canary=True,
        resources=SubmitResources(walltime_sec=walltime_sec, cpus=4, mpi=mpi),
    )


def _wt(spec: SubmitFlowSpec) -> int | None:
    assert spec.resources is not None
    return spec.resources.walltime_sec


def test_calibrated_base_shrinks_the_launched_walltime(tmp_path: Path) -> None:
    from hpc_agent.ops.submit_and_verify import _calibrated_base

    _write_canary_sidecar(tmp_path)
    stamp_canary_runtime(tmp_path, _CANARY_ID, elapsed_sec=600)  # 10-min task
    base = _base_spec(walltime_sec=21600)

    calibrated = _calibrated_base(tmp_path, base, _CANARY_ID)

    assert _wt(calibrated) == 1800  # 3× 600 floored, well under 6h
    # The original spec is not mutated in place.
    assert _wt(base) == 21600


def test_calibrated_base_noop_without_stamp(tmp_path: Path) -> None:
    from hpc_agent.ops.submit_and_verify import _calibrated_base

    _write_canary_sidecar(tmp_path)  # exists but no elapsed stamped
    base = _base_spec()
    assert _wt(_calibrated_base(tmp_path, base, _CANARY_ID)) == 21600


def test_calibrated_base_noop_without_canary_run_id(tmp_path: Path) -> None:
    from hpc_agent.ops.submit_and_verify import _calibrated_base

    base = _base_spec()
    assert _wt(_calibrated_base(tmp_path, base, None)) == 21600


def test_calibrated_base_skips_mpi(tmp_path: Path) -> None:
    """An MPI canary is a shrunk 2-rank probe — its wall-clock is unrepresentative."""
    from hpc_agent.ops.submit_and_verify import _calibrated_base

    _write_canary_sidecar(tmp_path)
    stamp_canary_runtime(tmp_path, _CANARY_ID, elapsed_sec=600)
    base = _base_spec(walltime_sec=21600, mpi=MpiSpec(ranks=8, launcher="srun"), total_tasks=1)
    assert _wt(_calibrated_base(tmp_path, base, _CANARY_ID)) == 21600  # untouched


# ── the S2 brief recomputes est off the calibrated walltime + discloses ──────


def _sv_result(canary_run_id: str | None = _CANARY_ID) -> SubmitAndVerifyResult:
    return SubmitAndVerifyResult(
        run_id=_RUN_ID,
        job_ids=[],
        total_tasks=900,
        deduped=False,
        canary_run_id=canary_run_id,
        canary_job_ids=["12344"],
        verified=True,
        failure_kind=None,
        verify_result=None,
    )


def test_s2_recomputes_est_off_calibrated_walltime_and_discloses(tmp_path: Path) -> None:
    """The consent footprint reflects the SHRUNK walltime, and the shrink is disclosed."""
    _write_canary_sidecar(tmp_path)
    stamp_canary_runtime(tmp_path, _CANARY_ID, elapsed_sec=600)
    spec = SubmitS2Spec(
        submit=SubmitAndVerifySpec(submit=_base_spec(), poll_interval_sec=1, wait_budget_sec=5),
        detach=False,
    )
    greenlight(tmp_path, "submit-s2", run_id=_RUN_ID)

    with mock.patch.object(blocks, "submit_and_verify", return_value=_sv_result()):
        result = blocks.submit_s2(tmp_path, spec=spec)

    # Calibrated: 900 tasks × 1800s × 4 cores / 3600 = 1800.0 core-hours,
    # NOT the 900 × 21600 × 4 / 3600 = 21600.0 the padded ceiling would inflate to.
    assert result.brief["est_core_hours"] == 1800.0
    calib = result.brief["walltime_calibration"]
    assert calib["applied"] is True
    assert calib["calibrated_walltime_sec"] == 1800
    assert calib["canary_elapsed_sec"] == 600
    assert "1800" in result.reason


def test_s2_uncalibrated_brief_is_byte_identical(tmp_path: Path) -> None:
    """No canary measurement → est unchanged and NO walltime_calibration key (a
    non-calibrating brief must not drift)."""
    # No canary sidecar written → read_canary_elapsed_sec returns None.
    spec = SubmitS2Spec(
        submit=SubmitAndVerifySpec(submit=_base_spec(), poll_interval_sec=1, wait_budget_sec=5),
        detach=False,
    )
    greenlight(tmp_path, "submit-s2", run_id=_RUN_ID)

    with mock.patch.object(blocks, "submit_and_verify", return_value=_sv_result()):
        result = blocks.submit_s2(tmp_path, spec=spec)

    assert result.brief["est_core_hours"] == 21600.0  # 900 × 21600 × 4 / 3600
    assert "walltime_calibration" not in result.brief
