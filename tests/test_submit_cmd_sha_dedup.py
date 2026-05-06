"""A5: regression — submit_and_record dedups via cmd_sha when the
journal has been wiped but the per-experiment sidecar at
``<exp>/.hpc/runs/<run_id>.json`` is still on disk.

Without the fallback path, the function would generate a fresh
RunRecord and the caller would re-submit a job the cluster already has
running.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from claude_hpc import register_primitives, runner
from claude_hpc._schema_models.submit import SubmitSpec as _WireSubmitSpec

# Register primitives BEFORE patching runner attrs (see test_runner.py
# for the same reason).
register_primitives()

if TYPE_CHECKING:
    from pathlib import Path


_SUBMIT_SPEC_FIELDS = {
    "profile", "cluster", "ssh_target", "remote_path", "job_name",
    "run_id", "job_ids", "total_tasks", "runtime", "campaign_id",
}
_real_submit_and_record = runner.submit_and_record


def _patched_submit_and_record(experiment_dir, **kwargs):
    """Test shim — same dual-path pattern used in test_runner."""
    if "spec" in kwargs:
        return _real_submit_and_record(experiment_dir, **kwargs)
    spec_kwargs = {k: v for k, v in kwargs.items() if k in _SUBMIT_SPEC_FIELDS}
    framework_kwargs = {k: v for k, v in kwargs.items() if k not in _SUBMIT_SPEC_FIELDS}
    if spec_kwargs.get("campaign_id") == "":
        spec_kwargs["campaign_id"] = None
    return _real_submit_and_record(
        experiment_dir, spec=_WireSubmitSpec(**spec_kwargs), **framework_kwargs
    )


runner.submit_and_record = _patched_submit_and_record


def _write_sidecar(experiment_dir: Path, run_id: str, **fields) -> Path:
    target = experiment_dir / ".hpc" / "runs" / f"{run_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "sidecar_schema_version": 2,
        "run_id": run_id,
        "cmd_sha": fields.pop("cmd_sha", "a" * 64),
        "claude_hpc_version": "0.2.0",
        "submitted_at": "2026-01-01T00:00:00Z",
        "executor": "python3 src/run.py",
        "result_dir_template": "results/{seed}",
        "task_count": fields.pop("task_count", 4),
        "tasks_py_sha": "1" * 64,
    }
    payload.update(fields)
    target.write_text(json.dumps(payload))
    return target


def test_cmd_sha_dedup_short_circuits_when_sidecar_exists(tmp_path: Path) -> None:
    """Journal is empty but sidecar with same cmd_sha exists -> dedup, no SSH."""
    cmd_sha = "f" * 64
    pre_existing_run_id = "20260101-000000-existin"

    _write_sidecar(
        tmp_path,
        pre_existing_run_id,
        cmd_sha=cmd_sha,
        profile="gpu-a100",
        cluster="discovery",
        ssh_target="me@cluster",
        remote_path="/scratch/exp",
        job_name="ml",
        job_ids=["12345"],
        task_count=4,
        campaign_id="",
    )

    record, deduped = runner.submit_and_record(
        tmp_path,
        profile="gpu-a100",
        cluster="discovery",
        ssh_target="me@cluster",
        remote_path="/scratch/exp",
        job_name="ml",
        run_id="20260102-000000-newone1",  # different from sidecar
        job_ids=["99999"],  # would be different
        total_tasks=4,
        cmd_sha=cmd_sha,
    )

    assert deduped is True
    # Got the OLD run_id back, not the one we passed in, so the caller
    # will skip the qsub.
    assert record.run_id == pre_existing_run_id
    assert record.job_ids == ["12345"]


def test_cmd_sha_dedup_no_op_when_no_match(tmp_path: Path) -> None:
    """Sidecar with DIFFERENT cmd_sha must not short-circuit."""
    _write_sidecar(tmp_path, "20260101-000000-other00", cmd_sha="b" * 64)
    record, deduped = runner.submit_and_record(
        tmp_path,
        profile="cpu",
        cluster="discovery",
        ssh_target="me@cluster",
        remote_path="/scratch/exp",
        job_name="ml",
        run_id="20260102-000000-fresh11",
        job_ids=["55555"],
        total_tasks=4,
        cmd_sha="c" * 64,  # mismatches the sidecar
    )
    assert deduped is False
    assert record.run_id == "20260102-000000-fresh11"


def test_cmd_sha_param_is_optional(tmp_path: Path) -> None:
    """Existing callers that do not pass cmd_sha must keep working."""
    record, deduped = runner.submit_and_record(
        tmp_path,
        profile="cpu",
        cluster="discovery",
        ssh_target="me@cluster",
        remote_path="/scratch/exp",
        job_name="ml",
        run_id="20260102-000000-noshahere",
        job_ids=["7"],
        total_tasks=1,
    )
    assert deduped is False
    assert record.run_id == "20260102-000000-noshahere"
