"""F-J residual — verify-reproduction's per-task compare honors summary_artifact.

``_load_partial_side`` (the design-center-5 per-task load path) hardcoded
``metrics.json`` when reading each compared task's summary. A reproduction whose
executor emits e.g. ``results_reduce.json`` would find NOTHING and count every
task UNCOMPARED. The seam now resolves each side's declared ``summary_artifact``
independently and threads it as ``filename``.

FIRES: a run emitting results_reduce.json is loaded present when the declared
name is threaded, and empty under the old metrics.json hardcode.
PASSES: an undeclared run resolves to metrics.json and loads byte-identical.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from hpc_agent.ops import verify_reproduction as vr

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "20260101-000000-reproart"


def _write_task_summary(
    experiment_dir: Path, run_id: str, idx: int, name: str, payload: dict
) -> None:
    task_dir = vr._partial_dir(experiment_dir, run_id) / str(idx)
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / name).write_text(json.dumps(payload), encoding="utf-8")


def test_fires_declared_summary_artifact_loads_present(tmp_path: Path) -> None:
    """FIRES: a per-task compare over results_reduce.json.

    Threading the declared name loads the task present + flattens its metrics;
    the old metrics.json hardcode finds nothing → UNCOMPARED (the F-J gap).
    """
    _write_task_summary(tmp_path, _RUN_ID, 0, "results_reduce.json", {"loss": 2.0})

    # Declared name honored → present + comparable.
    flat, present = vr._load_partial_side(tmp_path, _RUN_ID, [0], filename="results_reduce.json")
    assert present == [0]
    assert flat == {"task0.loss": 2.0}

    # Old hardcode (metrics.json) against the SAME on-disk state → nothing found.
    flat_hc, present_hc = vr._load_partial_side(tmp_path, _RUN_ID, [0], filename="metrics.json")
    assert present_hc == []
    assert flat_hc == {}


def test_default_metrics_json_unchanged(tmp_path: Path) -> None:
    """PASSES: an undeclared run (resolved default metrics.json) loads as before."""
    _write_task_summary(tmp_path, _RUN_ID, 0, "metrics.json", {"loss": 2.0})
    flat, present = vr._load_partial_side(tmp_path, _RUN_ID, [0], filename="metrics.json")
    assert present == [0]
    assert flat == {"task0.loss": 2.0}
