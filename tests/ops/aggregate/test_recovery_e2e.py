"""End-to-end recovery integrity (F06 + F07, compounding).

The worst outcome in the aggregate surface: a wave combines while some tasks
never wrote metrics, the completeness gate reports green anyway (F07), and a
resubmit that fixes the tasks never re-runs the combiner so the recovered
numbers stay excluded (F06). This drives the REAL combiner script, the REAL
completeness invariant, and the REAL local reduce to prove:

  * ``all_tasks_present`` FAILS while the 40 recovered tasks are missing, and
    passes only once they are aggregated;
  * the aggregate NUMBERS change to include the recovered tasks after the
    forced recombine.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from hpc_agent.execution.mapreduce import combiner as combiner_mod
from hpc_agent.execution.mapreduce.combiner import main as combiner_main
from hpc_agent.execution.mapreduce.reduce.metrics import reduce_partials
from hpc_agent.ops.aggregate.invariants import verify_aggregation_complete

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "e2e_run"
_N = 100
_PRESENT = 60  # tasks that wrote metrics before the recovery
_SCORE_PRESENT = 1.0
_SCORE_RECOVERED = 6.0


def _write_metrics(result_root, tid, score):
    d = result_root / f"task_{tid}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "metrics.json").write_text(json.dumps({"score": score, "n_samples": 1}))


def _combine(tmp_path, monkeypatch, *, force):
    _patch = str(tmp_path / ".hpc" / "_hpc_combiner.py")
    monkeypatch.setattr(combiner_mod, "__file__", _patch, raising=False)
    monkeypatch.chdir(tmp_path)
    argv = ["--wave", "0", "--run-id", _RUN_ID]
    if force:
        argv.append("--force")
    combiner_main(argv=argv)
    return json.loads((tmp_path / "_combiner" / _RUN_ID / "wave_0.json").read_text())


def test_recover_then_reaggregate_includes_recovered_tasks(tmp_path: Path, monkeypatch) -> None:
    from tests.conftest import make_sidecar_json, write_hpc_tasks

    hpc = tmp_path / ".hpc"
    # All tasks share one grid point so the aggregate is a single moving number.
    write_hpc_tasks(hpc, [{"model": "m"}] * _N)
    make_sidecar_json(
        tmp_path,
        run_id=_RUN_ID,
        result_dir_template=str(tmp_path / "results" / "task_{task_id}"),
        task_count=_N,
        wave_map={"0": list(range(_N))},
    )

    result_root = tmp_path / "results"
    # Only the first 60 tasks wrote metrics; 40 are missing (died / NFS lag).
    for tid in range(_PRESENT):
        _write_metrics(result_root, tid, _SCORE_PRESENT)

    # --- First combine over the partial wave ---
    partial = _combine(tmp_path, monkeypatch, force=False)
    assert partial["task_ids"] == list(range(_N))  # full membership echoed
    assert partial["tasks_read"] == list(range(_PRESENT))  # only 60 aggregated
    assert len(partial["errors"]) == _N - _PRESENT

    combiner_local = tmp_path / "_combiner"

    # F07: the gate FAILS — 40 tasks never landed in the aggregate.
    pre = verify_aggregation_complete(tmp_path, run_id=_RUN_ID, combiner_dir_local=combiner_local)
    assert pre["all_tasks_present"] is False
    assert pre["missing_tasks"] == list(range(_PRESENT, _N))
    assert pre["ok"] is False

    # The aggregate number reflects only the 60 present tasks.
    pre_num = reduce_partials(combiner_local, run_id=_RUN_ID)
    (pre_key,) = pre_num.keys()
    assert abs(pre_num[pre_key]["score"] - _SCORE_PRESENT) < 1e-9

    # --- Recover: the 40 resubmitted tasks now write their metrics ---
    for tid in range(_PRESENT, _N):
        _write_metrics(result_root, tid, _SCORE_RECOVERED)

    # F06: a FORCE recombine (what the invalidation drives on the next pass)
    # re-runs the combiner over the recovered data.
    full = _combine(tmp_path, monkeypatch, force=True)
    assert full["tasks_read"] == list(range(_N))
    assert full["errors"] == []

    # F07 again: the gate now passes.
    post = verify_aggregation_complete(tmp_path, run_id=_RUN_ID, combiner_dir_local=combiner_local)
    assert post["all_tasks_present"] is True
    assert post["missing_tasks"] == []

    # The NUMBERS moved to include the recovered tasks:
    # (60*1.0 + 40*6.0) / 100 = 3.0.
    post_num = reduce_partials(combiner_local, run_id=_RUN_ID)
    (post_key,) = post_num.keys()
    expected = (_PRESENT * _SCORE_PRESENT + (_N - _PRESENT) * _SCORE_RECOVERED) / _N
    assert abs(post_num[post_key]["score"] - expected) < 1e-9
    assert abs(post_num[post_key]["score"] - pre_num[pre_key]["score"]) > 1.0  # actually changed
