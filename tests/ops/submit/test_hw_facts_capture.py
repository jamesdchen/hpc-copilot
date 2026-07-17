"""The canary's hardware-facts capture — U-HW1 (reproducibility gap #5).

The dispatcher already emitted the placement facts into the canary's per-task
``_runtime.json`` (node / cpu_model / partition), and the fingerprint pull brought
it home; ``capture_and_stamp_hw_facts`` READS that already-landed file (via an
INJECTED load here, NO SSH — the contrast with env-lock) and stamps the reduced
``hw_sha`` on the MAIN run's sidecar. Covers:

* the runtime carries facts → ``hw_sha`` + facts + ``captured`` status stamped;
* an ABSENT / empty runtime → an honest ``could_not_capture``, never a silent skip;
* the ``_runtime.json`` → fact-key projection (``partition`` from the runtime key).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hpc_agent.ops.submit.hw_facts_capture import (
    RUNTIME_SIDECAR_NAME,
    capture_and_stamp_hw_facts,
    facts_from_runtime,
)
from hpc_agent.state.hw_facts import STATUS_CAPTURED, STATUS_COULD_NOT_CAPTURE, hw_sha
from hpc_agent.state.runs import read_run_sidecar, write_run_sidecar

RUNTIME = {
    "task_id": 0,
    "run_id": "run-x-canary",
    "node": "gpu-a-01",
    "cpu_model": "Widget Xeon Gold 6248",
    "partition": "gpu",
    "elapsed_sec": 12,
}
FACTS = {"node": "gpu-a-01", "cpu_model": "Widget Xeon Gold 6248", "partition": "gpu"}


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


def test_runtime_sidecar_name_mirrors_dispatcher() -> None:
    """Pin the ``_runtime.json`` filename across the standalone boundary (N7).

    The dispatcher ships to the compute node WITHOUT ``hpc_agent`` importable, so
    it writes the runtime-sidecar filename as a bare literal; the control-plane
    readers (this module + submit_and_verify) name it as a constant. This pins the
    three in lock-step: a rename on either side fails HERE, not silently in
    production (the capture would stop finding the file).
    """
    import hpc_agent.execution.mapreduce.dispatch as dispatch_mod
    from hpc_agent.ops.submit_and_verify import _RUNTIME_SIDECAR_NAME

    dispatch_src = Path(dispatch_mod.__file__).read_text(encoding="utf-8")
    assert RUNTIME_SIDECAR_NAME == "_runtime.json"
    assert _RUNTIME_SIDECAR_NAME == RUNTIME_SIDECAR_NAME
    # The dispatcher writes this exact filename (the standalone-boundary literal).
    assert f'"{RUNTIME_SIDECAR_NAME}"' in dispatch_src


def test_facts_from_runtime_projects_placement_keys() -> None:
    # Only the placement fields are projected onto the vocabulary; telemetry
    # (task_id / elapsed_sec) is dropped.
    assert facts_from_runtime(RUNTIME) == FACTS
    assert facts_from_runtime(None) == {}
    assert facts_from_runtime({}) == {}


def test_canary_runtime_stamps_hw_facts(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, "run-x")

    def _load(*, canary_run_id: str) -> dict[str, Any]:
        assert canary_run_id == "run-x"
        return RUNTIME

    snap = capture_and_stamp_hw_facts(tmp_path, run_id="run-x", canary_run_id="run-x", load=_load)
    assert snap.resolved and snap.status == STATUS_CAPTURED
    sc = read_run_sidecar(tmp_path, "run-x")
    assert sc["hw_sha"] == hw_sha(FACTS)
    assert sc["hw_facts"] == FACTS
    assert sc["hw_status"] == STATUS_CAPTURED


def test_absent_runtime_stamps_could_not_capture(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, "run-x")

    def _none(*, canary_run_id: str) -> dict[str, Any] | None:
        return None  # _runtime.json did not come home (old wheel / no pull)

    snap = capture_and_stamp_hw_facts(tmp_path, run_id="run-x", canary_run_id="run-x", load=_none)
    assert not snap.resolved and snap.status == STATUS_COULD_NOT_CAPTURE
    sc = read_run_sidecar(tmp_path, "run-x")
    # No-silent-caps: the could-not-capture verdict is recorded (null sha/facts).
    assert sc["hw_sha"] is None and sc["hw_facts"] is None
    assert sc["hw_status"] == STATUS_COULD_NOT_CAPTURE


def test_load_raise_degrades_to_could_not_capture(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, "run-x")

    def _boom(*, canary_run_id: str) -> dict[str, Any]:
        raise RuntimeError("read exploded")

    # Best-effort: a raising load never propagates; it records could_not_capture.
    snap = capture_and_stamp_hw_facts(tmp_path, run_id="run-x", canary_run_id="run-x", load=_boom)
    assert snap.status == STATUS_COULD_NOT_CAPTURE
    assert read_run_sidecar(tmp_path, "run-x")["hw_status"] == STATUS_COULD_NOT_CAPTURE


def test_empty_placement_in_runtime_is_could_not_capture(tmp_path: Path) -> None:
    # A runtime that carried the keys but ALL blank (a node with no cpuinfo and no
    # scheduler vars) records could-not-capture, not a sha over nothing.
    _write_sidecar(tmp_path, "run-x")

    def _blank(*, canary_run_id: str) -> dict[str, Any]:
        return {"node": "", "cpu_model": "", "partition": ""}

    snap = capture_and_stamp_hw_facts(tmp_path, run_id="run-x", canary_run_id="run-x", load=_blank)
    assert snap.status == STATUS_COULD_NOT_CAPTURE
    assert read_run_sidecar(tmp_path, "run-x")["hw_sha"] is None
