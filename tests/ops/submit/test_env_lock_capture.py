"""The canary's env-lock capture — U-ENV1 (reproducibility program).

The canary already ran a real task under the run's env; ``capture_and_stamp_env_lock``
resolves that env (via an INJECTED fetch here, no SSH) and stamps the reduced
``env_lock_sha`` on the MAIN run's sidecar. Covers the two required cases:

* the canary EMITS a snapshot → ``env_lock_sha`` + ``captured`` status stamped;
* an ABSENT snapshot → an honest ``could_not_capture`` status, never a silent skip.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hpc_agent.ops.submit.env_lock_capture import capture_and_stamp_env_lock
from hpc_agent.state.env_lock import STATUS_CAPTURED, STATUS_COULD_NOT_CAPTURE, env_lock_sha
from hpc_agent.state.runs import read_run_sidecar, write_run_sidecar

PIP = "widget==1.0\ngadget==2.0\n"


def _write_sidecar(exp: Path, run_id: str, **over: Any) -> None:
    kwargs: dict[str, Any] = {
        "run_id": run_id,
        "cmd_sha": "a" * 64,
        "hpc_agent_version": "0.11.0",
        "submitted_at": "2026-01-01T00:00:00Z",
        "executor": "python train.py",
        "result_dir_template": "results/{task_id}",
        "task_count": 1,
        "tasks_py_sha": "b" * 64,
        "cluster": "widgetcluster",
    }
    kwargs.update(over)
    write_run_sidecar(exp, **kwargs)


def test_canary_emits_and_stamps_env_lock(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, "run-x")

    def _fetch(*, canary_run_id: str) -> dict[str, str]:
        assert canary_run_id == "run-x-canary"
        return {"pip_freeze": PIP}

    snap = capture_and_stamp_env_lock(
        tmp_path, run_id="run-x", canary_run_id="run-x-canary", fetch=_fetch
    )
    assert snap.resolved and snap.source == "pip_freeze"
    sc = read_run_sidecar(tmp_path, "run-x")
    assert sc["env_lock_sha"] == env_lock_sha("pip_freeze", PIP)
    assert sc["env_lock_status"] == STATUS_CAPTURED


def test_absent_snapshot_stamps_could_not_capture(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, "run-x")

    def _empty_fetch(*, canary_run_id: str) -> dict[str, str]:
        return {}  # the env could not be resolved (nothing came back)

    snap = capture_and_stamp_env_lock(
        tmp_path, run_id="run-x", canary_run_id="run-x-canary", fetch=_empty_fetch
    )
    assert not snap.resolved and snap.status == STATUS_COULD_NOT_CAPTURE
    sc = read_run_sidecar(tmp_path, "run-x")
    # No-silent-caps: the could-not-capture verdict is recorded (null sha).
    assert sc["env_lock_sha"] is None
    assert sc["env_lock_status"] == STATUS_COULD_NOT_CAPTURE


def test_fetch_raise_degrades_to_could_not_capture(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, "run-x")

    def _boom(*, canary_run_id: str) -> dict[str, str]:
        raise RuntimeError("ssh exploded")

    # Best-effort: a raising fetch never propagates; it records could_not_capture.
    snap = capture_and_stamp_env_lock(
        tmp_path, run_id="run-x", canary_run_id="run-x-canary", fetch=_boom
    )
    assert snap.status == STATUS_COULD_NOT_CAPTURE
    assert read_run_sidecar(tmp_path, "run-x")["env_lock_status"] == STATUS_COULD_NOT_CAPTURE
