"""Tests for ``hpc_agent.ops.aggregate.invariants``."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent.ops.aggregate.invariants import (
    check_result_columns,
    verify_aggregation_complete,
)
from hpc_agent.state.runs import write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path


def _seed_sidecar_with_wave_map(
    experiment: Path,
    run_id: str,
    wave_map: dict,
    *,
    results: dict | None = None,
) -> None:
    write_run_sidecar(
        experiment,
        run_id=run_id,
        cmd_sha="0" * 64,
        hpc_agent_version="0.2.0",
        submitted_at="2026-01-01T00:00:00Z",
        executor="python3 src/run.py",
        result_dir_template="results/{seed}",
        task_count=sum(len(v) for v in wave_map.values()),
        tasks_py_sha="1" * 64,
        wave_map=wave_map,
        results=results,
    )


def _write_wave_partial(combiner_dir: Path, wave: int, run_id: str, task_ids: list[int]) -> None:
    combiner_dir.mkdir(parents=True, exist_ok=True)
    (combiner_dir / f"wave_{wave}.json").write_text(
        json.dumps(
            {
                "wave": wave,
                "run_id": run_id,
                "task_ids": task_ids,
                "grid_points": {},
                "errors": [],
            }
        )
    )


def test_all_invariants_pass(tmp_path: Path) -> None:
    _seed_sidecar_with_wave_map(tmp_path, "r1", {"0": [0, 1, 2], "1": [3, 4, 5]})
    combiner_dir = tmp_path / "_combiner_local"
    _write_wave_partial(combiner_dir, 0, "r1", [0, 1, 2])
    _write_wave_partial(combiner_dir, 1, "r1", [3, 4, 5])

    out = verify_aggregation_complete(tmp_path, run_id="r1", combiner_dir_local=combiner_dir)
    assert out["ok"] is True
    assert out["all_waves_combined"] is True
    assert out["all_tasks_present"] is True
    assert out["provenance_present"] is True
    assert out["missing_waves"] == []
    assert out["missing_tasks"] == []
    assert out["unexpected_tasks"] == []


def test_missing_wave_partial(tmp_path: Path) -> None:
    _seed_sidecar_with_wave_map(tmp_path, "r1", {"0": [0, 1], "1": [2, 3]})
    combiner_dir = tmp_path / "_combiner_local"
    _write_wave_partial(combiner_dir, 0, "r1", [0, 1])
    # wave 1 missing.

    out = verify_aggregation_complete(tmp_path, run_id="r1", combiner_dir_local=combiner_dir)
    assert out["ok"] is False
    assert out["all_waves_combined"] is False
    assert out["missing_waves"] == [1]
    assert out["missing_tasks"] == [2, 3]


def test_unexpected_task_in_partial(tmp_path: Path) -> None:
    """Cross-run contamination — task 99 appears in our partial but isn't in our wave_map."""
    _seed_sidecar_with_wave_map(tmp_path, "r1", {"0": [0, 1, 2]})
    combiner_dir = tmp_path / "_combiner_local"
    _write_wave_partial(combiner_dir, 0, "r1", [0, 1, 2, 99])

    out = verify_aggregation_complete(tmp_path, run_id="r1", combiner_dir_local=combiner_dir)
    assert out["ok"] is False
    assert out["unexpected_tasks"] == [99]


def test_wrong_provenance_run_id(tmp_path: Path) -> None:
    _seed_sidecar_with_wave_map(tmp_path, "r1", {"0": [0, 1]})
    combiner_dir = tmp_path / "_combiner_local"
    # Partial says it's from a different run.
    _write_wave_partial(combiner_dir, 0, "OTHER_RUN", [0, 1])

    out = verify_aggregation_complete(tmp_path, run_id="r1", combiner_dir_local=combiner_dir)
    assert out["ok"] is False
    assert out["provenance_present"] is False


def test_wrong_provenance_wave_field(tmp_path: Path) -> None:
    """A partial whose internal ``wave`` field disagrees with its filename
    (``wave_<N>.json``) must fail provenance — the check keys on the filename
    wave, not the doc's own field, so a mislabeled internal wave is caught."""
    _seed_sidecar_with_wave_map(tmp_path, "r1", {"0": [0, 1]})
    combiner_dir = tmp_path / "_combiner_local"
    combiner_dir.mkdir(parents=True, exist_ok=True)
    # File is wave_0.json but its body claims wave 7.
    (combiner_dir / "wave_0.json").write_text(
        json.dumps({"wave": 7, "run_id": "r1", "task_ids": [0, 1]})
    )

    out = verify_aggregation_complete(tmp_path, run_id="r1", combiner_dir_local=combiner_dir)
    assert out["provenance_present"] is False


def test_runtime_sidecar_skipped_in_walk(tmp_path: Path) -> None:
    """wave_*.runtime.json files (warm-picker pipeline) must not be parsed as wave partials."""
    _seed_sidecar_with_wave_map(tmp_path, "r1", {"0": [0]})
    combiner_dir = tmp_path / "_combiner_local"
    _write_wave_partial(combiner_dir, 0, "r1", [0])
    # Adversarial: a runtime sidecar with a different shape.
    (combiner_dir / "wave_0.runtime.json").write_text(
        json.dumps({"wave": 0, "run_id": "r1", "samples": []})
    )

    out = verify_aggregation_complete(tmp_path, run_id="r1", combiner_dir_local=combiner_dir)
    assert out["ok"] is True


def test_missing_sidecar_raises(tmp_path: Path) -> None:
    combiner_dir = tmp_path / "_combiner_local"
    combiner_dir.mkdir()
    with pytest.raises(errors.SpecInvalid):
        verify_aggregation_complete(tmp_path, run_id="missing", combiner_dir_local=combiner_dir)


def test_non_directory_combiner_dir_raises(tmp_path: Path) -> None:
    _seed_sidecar_with_wave_map(tmp_path, "r1", {"0": [0]})
    with pytest.raises(errors.SpecInvalid, match="not a directory"):
        verify_aggregation_complete(
            tmp_path, run_id="r1", combiner_dir_local=tmp_path / "no_such_dir"
        )


def test_empty_run_id_raises(tmp_path: Path) -> None:
    combiner_dir = tmp_path / "x"
    combiner_dir.mkdir()
    with pytest.raises(errors.SpecInvalid, match="run_id"):
        verify_aggregation_complete(tmp_path, run_id="", combiner_dir_local=combiner_dir)


# ---------------------------------------------------------------------------
# Check 2 — expected columns + non-NaN metric (check_result_columns)
# ---------------------------------------------------------------------------


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [",".join(header)]
    lines.extend(",".join(r) for r in rows)
    path.write_text("\n".join(lines) + "\n")


def test_check_result_columns_no_schema_is_noop(tmp_path: Path) -> None:
    _write_csv(tmp_path / "task_1" / "out.csv", ["a", "b"], [["1", "2"]])
    out = check_result_columns(tmp_path)
    assert out["checked"] is False
    assert out["ok"] is True
    assert out["violations"] == []


def test_check_result_columns_all_pass(tmp_path: Path) -> None:
    _write_csv(tmp_path / "task_1" / "out.csv", ["seed", "qlike"], [["7", "0.42"]])
    _write_csv(tmp_path / "task_2" / "out.csv", ["seed", "qlike"], [["8", "0.51"]])
    out = check_result_columns(tmp_path, expected_columns=["seed", "qlike"], metric_column="qlike")
    assert out["checked"] is True
    assert out["ok"] is True
    assert out["files_scanned"] == 2
    assert out["violations"] == []


def test_check_result_columns_missing_column(tmp_path: Path) -> None:
    _write_csv(tmp_path / "task_1" / "out.csv", ["seed"], [["7"]])
    out = check_result_columns(tmp_path, expected_columns=["seed", "qlike"], metric_column=None)
    assert out["ok"] is False
    assert out["violations"][0]["missing_columns"] == ["qlike"]


def test_check_result_columns_nan_metric(tmp_path: Path) -> None:
    # Blank cell, literal NaN, and a non-numeric token all count as NaN.
    _write_csv(
        tmp_path / "task_1" / "out.csv",
        ["seed", "qlike"],
        [["7", "0.42"], ["8", ""], ["9", "NaN"], ["10", "err"]],
    )
    out = check_result_columns(tmp_path, expected_columns=["seed", "qlike"], metric_column="qlike")
    assert out["ok"] is False
    v = out["violations"][0]
    assert v["metric_nan"] is True
    assert v["metric_nan_rows"] == [2, 3, 4]


def test_check_result_columns_metric_column_absent(tmp_path: Path) -> None:
    _write_csv(tmp_path / "task_1" / "out.csv", ["seed"], [["7"]])
    out = check_result_columns(tmp_path, metric_column="qlike")
    assert out["ok"] is False
    assert "qlike" in out["violations"][0]["missing_columns"]


def test_check_result_columns_empty_file(tmp_path: Path) -> None:
    (tmp_path / "task_1").mkdir(parents=True)
    (tmp_path / "task_1" / "out.csv").write_text("")
    out = check_result_columns(tmp_path, metric_column="qlike")
    assert out["ok"] is False
    assert out["violations"][0]["error"] is not None


def test_verify_aggregation_complete_runs_column_gate(tmp_path: Path) -> None:
    """The columns gate fires when the sidecar declares a results schema."""
    _seed_sidecar_with_wave_map(
        tmp_path,
        "r1",
        {"0": [0, 1]},
        results={"expected_columns": ["seed", "qlike"], "metric_column": "qlike"},
    )
    combiner_dir = tmp_path / "_combiner_local"
    _write_wave_partial(combiner_dir, 0, "r1", [0, 1])
    results_dir = tmp_path / "summaries"
    _write_csv(results_dir / "task_1" / "out.csv", ["seed", "qlike"], [["7", "0.4"]])
    _write_csv(results_dir / "task_2" / "out.csv", ["seed", "qlike"], [["8", ""]])

    out = verify_aggregation_complete(
        tmp_path,
        run_id="r1",
        combiner_dir_local=combiner_dir,
        results_dir_local=results_dir,
    )
    assert out["columns_checked"] is True
    assert out["ok"] is False
    assert len(out["column_violations"]) == 1
    assert out["column_violations"][0]["metric_nan"] is True


def test_verify_aggregation_complete_column_gate_noop_without_schema(tmp_path: Path) -> None:
    """No `results` block declared -> columns gate is a clean skip."""
    _seed_sidecar_with_wave_map(tmp_path, "r1", {"0": [0]})
    combiner_dir = tmp_path / "_combiner_local"
    _write_wave_partial(combiner_dir, 0, "r1", [0])
    results_dir = tmp_path / "summaries"
    _write_csv(results_dir / "task_1" / "out.csv", ["seed"], [["7"]])

    out = verify_aggregation_complete(
        tmp_path,
        run_id="r1",
        combiner_dir_local=combiner_dir,
        results_dir_local=results_dir,
    )
    assert out["columns_checked"] is False
    assert out["column_violations"] == []
    assert out["ok"] is True


def test_verify_aggregation_complete_column_gate_noop_without_results_dir(
    tmp_path: Path,
) -> None:
    """Schema declared but no results dir supplied -> clean skip."""
    _seed_sidecar_with_wave_map(
        tmp_path,
        "r1",
        {"0": [0]},
        results={"expected_columns": ["seed"], "metric_column": None},
    )
    combiner_dir = tmp_path / "_combiner_local"
    _write_wave_partial(combiner_dir, 0, "r1", [0])

    out = verify_aggregation_complete(tmp_path, run_id="r1", combiner_dir_local=combiner_dir)
    assert out["columns_checked"] is False
    assert out["ok"] is True
