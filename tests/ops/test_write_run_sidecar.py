"""Tests for the ``write-run-sidecar`` primitive.

Covers the agent-facing CLI wrapper around
:func:`hpc_agent.state.runs.write_run_sidecar`: happy path, the #162
dispatcher-executor refusal that prevents self-recursive sidecars at
the new CLI surface, idempotent overwrite, and v2 config-snapshot
round-trip.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.write_run_sidecar import WriteRunSidecarInput
from hpc_agent.ops.write_run_sidecar import write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path


def _spec(**overrides: object) -> WriteRunSidecarInput:
    """Build a minimal valid ``WriteRunSidecarInput`` with overrides."""
    base: dict[str, object] = {
        "run_id": "20260101-000000-deadbee",
        "cmd_sha": "0" * 64,
        "executor": "python3 src/run.py --seed $SEED",
        "result_dir_template": "results/{seed}",
        "task_count": 4,
        "tasks_py_sha": "1" * 64,
    }
    base.update(overrides)
    return WriteRunSidecarInput.model_validate(base)


def test_happy_path_writes_sidecar_and_returns_path(tmp_path: Path) -> None:
    spec = _spec()
    out = write_run_sidecar(experiment_dir=tmp_path, spec=spec)

    target = tmp_path / ".hpc" / "runs" / f"{spec.run_id}.json"
    assert out == {"path": str(target)}
    assert target.is_file()
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["run_id"] == spec.run_id
    assert data["executor"] == spec.executor
    assert data["task_count"] == 4
    # Auto-stamped fields are present and non-empty.
    assert data["submitted_at"]
    assert data["hpc_agent_version"]


def test_dispatcher_executor_refused(tmp_path: Path) -> None:
    spec = _spec(executor="python3 .hpc/_hpc_dispatch.py")
    with pytest.raises(errors.SpecInvalid, match="dispatcher"):
        write_run_sidecar(experiment_dir=tmp_path, spec=spec)
    # Nothing should be written when the guard fires.
    target = tmp_path / ".hpc" / "runs" / f"{spec.run_id}.json"
    assert not target.exists()


def test_dispatcher_plain_dispatch_py_refused(tmp_path: Path) -> None:
    # The ``_is_runnable_executor`` predicate also matches bare ``dispatch.py``
    # (not just ``_hpc_dispatch.py``) — confirm the broader guard fires too.
    spec = _spec(executor="python3 dispatch.py")
    with pytest.raises(errors.SpecInvalid):
        write_run_sidecar(experiment_dir=tmp_path, spec=spec)


def test_idempotent_overwrite_produces_same_content(tmp_path: Path) -> None:
    spec = _spec()
    out1 = write_run_sidecar(experiment_dir=tmp_path, spec=spec)
    first = json.loads(Path(out1["path"]).read_text(encoding="utf-8"))

    # Drop the auto-stamped ``submitted_at`` — it's clock-dependent — and
    # confirm every other key matches byte-for-byte on a second write.
    out2 = write_run_sidecar(experiment_dir=tmp_path, spec=spec)
    second = json.loads(Path(out2["path"]).read_text(encoding="utf-8"))

    assert out1 == out2
    first.pop("submitted_at", None)
    second.pop("submitted_at", None)
    assert first == second


def test_v2_fields_round_trip_to_disk(tmp_path: Path) -> None:
    spec = _spec(
        cluster="hoffman2",
        profile="ml_ridge",
        resources={"cpus": 4, "mem": "16G", "walltime": "02:00:00"},
    )
    out = write_run_sidecar(experiment_dir=tmp_path, spec=spec)
    data = json.loads(Path(out["path"]).read_text(encoding="utf-8"))

    assert data["cluster"] == "hoffman2"
    assert data["profile"] == "ml_ridge"
    assert data["resources"] == {"cpus": 4, "mem": "16G", "walltime": "02:00:00"}


def test_trial_tokens_persist_to_disk(tmp_path: Path) -> None:
    """trial_tokens threaded through the primitive (from compute-run-id) land
    on the sidecar verbatim, completing the CLI round-trip to prior_records."""
    spec = _spec(campaign_id="tune_q1", trial_tokens=[10, 11, 12])
    out = write_run_sidecar(experiment_dir=tmp_path, spec=spec)
    data = json.loads(Path(out["path"]).read_text(encoding="utf-8"))
    assert data["trial_tokens"] == [10, 11, 12]


def test_trial_tokens_omitted_when_absent(tmp_path: Path) -> None:
    """Ordinary submit (no tokens) leaves the key off the on-disk JSON."""
    out = write_run_sidecar(experiment_dir=tmp_path, spec=_spec())
    data = json.loads(Path(out["path"]).read_text(encoding="utf-8"))
    assert "trial_tokens" not in data


# Import Path lazily for the runtime branches above (TYPE_CHECKING gate).
from pathlib import Path  # noqa: E402
