"""Tests for ``hpc_agent.atoms.validate_input_dataset``.

Pattern: write a tiny dataset to ``tmp_path`` (csv or jsonl —
parquet path is exercised separately when pyarrow is available),
call the validator, assert the findings.

Catches the NaN-trap bug class without needing pandas/pyarrow as a
test-time dep: the csv and jsonl loaders are stdlib-only, and the
parquet loader is exercised only when pyarrow is installed.
"""

from __future__ import annotations

import csv
import json
from typing import TYPE_CHECKING

import pytest

from hpc_agent._schema_models.validators.validate_input_dataset import (
    ValidateInputDatasetSpec,
)
from hpc_agent.atoms.validate_input_dataset import validate_input_dataset

if TYPE_CHECKING:
    from pathlib import Path


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


# ─── happy path: indices in bounds, columns non-null ─────────────────


def test_csv_clean_pass_emits_no_findings(tmp_path: Path) -> None:
    p = tmp_path / "data.csv"
    _write_csv(p, [{"target": "a", "x": "1"}, {"target": "b", "x": "2"}])
    out = validate_input_dataset(
        tmp_path,
        spec=ValidateInputDatasetSpec(
            dataset_path=str(p),
            loader="csv",
            row_indices=[0, 1],
            required_non_null_cols=["target"],
        ),
    )
    assert out.findings == []


def test_jsonl_clean_pass_emits_no_findings(tmp_path: Path) -> None:
    p = tmp_path / "data.jsonl"
    _write_jsonl(p, [{"target": 1.0}, {"target": 2.0}])
    out = validate_input_dataset(
        tmp_path,
        spec=ValidateInputDatasetSpec(
            dataset_path=str(p),
            loader="jsonl",
            row_indices=[0, 1],
            required_non_null_cols=["target"],
        ),
    )
    assert out.findings == []


# ─── row_index_oob ────────────────────────────────────────────────────


def test_csv_row_index_out_of_bounds_emits_error(tmp_path: Path) -> None:
    p = tmp_path / "data.csv"
    _write_csv(p, [{"x": "1"}, {"x": "2"}])
    out = validate_input_dataset(
        tmp_path,
        spec=ValidateInputDatasetSpec(
            dataset_path=str(p),
            loader="csv",
            row_indices=[0, 1, 99],  # 99 doesn't exist
        ),
    )
    finding = next(f for f in out.findings if f.code == "row_index_oob")
    assert finding.severity == "error"
    assert finding.evidence["row_index"] == 99
    assert finding.evidence["n_rows"] == 2


def test_jsonl_row_index_out_of_bounds_emits_error(tmp_path: Path) -> None:
    p = tmp_path / "data.jsonl"
    _write_jsonl(p, [{"x": 1}])
    out = validate_input_dataset(
        tmp_path,
        spec=ValidateInputDatasetSpec(dataset_path=str(p), loader="jsonl", row_indices=[5]),
    )
    assert any(f.code == "row_index_oob" for f in out.findings)


# ─── required_column_null (the NaN-trap bug class) ────────────────────


def test_csv_empty_string_in_required_col_emits_error(tmp_path: Path) -> None:
    """csv: empty-string is the on-disk representation of null."""
    p = tmp_path / "data.csv"
    _write_csv(
        p,
        [
            {"target": "1", "x": "a"},
            {"target": "", "x": "b"},  # NaN-trap row
            {"target": "3", "x": "c"},
        ],
    )
    out = validate_input_dataset(
        tmp_path,
        spec=ValidateInputDatasetSpec(
            dataset_path=str(p),
            loader="csv",
            row_indices=[0, 1, 2],
            required_non_null_cols=["target"],
        ),
    )
    finding = next(f for f in out.findings if f.code == "required_column_null")
    assert finding.severity == "error"
    assert finding.evidence["row_index"] == 1
    assert finding.evidence["column"] == "target"


def test_jsonl_explicit_null_in_required_col_emits_error(tmp_path: Path) -> None:
    p = tmp_path / "data.jsonl"
    _write_jsonl(p, [{"target": 1.0}, {"target": None}, {"target": 2.0}])
    out = validate_input_dataset(
        tmp_path,
        spec=ValidateInputDatasetSpec(
            dataset_path=str(p),
            loader="jsonl",
            row_indices=[0, 1, 2],
            required_non_null_cols=["target"],
        ),
    )
    assert any(
        f.code == "required_column_null" and f.evidence["row_index"] == 1 for f in out.findings
    )


def test_jsonl_missing_required_col_emits_error(tmp_path: Path) -> None:
    """If the column is entirely absent on a row (not None, just
    missing), it's null-ish from the campaign's perspective."""
    p = tmp_path / "data.jsonl"
    _write_jsonl(p, [{"target": 1.0}, {"other": "x"}, {"target": 2.0}])
    out = validate_input_dataset(
        tmp_path,
        spec=ValidateInputDatasetSpec(
            dataset_path=str(p),
            loader="jsonl",
            row_indices=[0, 1, 2],
            required_non_null_cols=["target"],
        ),
    )
    assert any(
        f.code == "required_column_null" and f.evidence["row_index"] == 1 for f in out.findings
    )


# ─── error paths ──────────────────────────────────────────────────────


def test_dataset_missing_emits_error(tmp_path: Path) -> None:
    out = validate_input_dataset(
        tmp_path,
        spec=ValidateInputDatasetSpec(
            dataset_path="nonexistent.csv", loader="csv", row_indices=[0]
        ),
    )
    finding = next(f for f in out.findings if f.code == "dataset_missing")
    assert finding.severity == "error"


def test_relative_path_resolves_against_experiment_dir(tmp_path: Path) -> None:
    """When ``dataset_path`` is relative, it resolves against
    *experiment_dir* — same convention as tasks_py_path. Pinning so a
    future "let me always require absolute" refactor breaks here."""
    p = tmp_path / "data.csv"
    _write_csv(p, [{"x": "1"}])
    out = validate_input_dataset(
        tmp_path,
        spec=ValidateInputDatasetSpec(dataset_path="data.csv", loader="csv", row_indices=[0]),
    )
    assert out.findings == []


# ─── parquet (only if pyarrow installed) ──────────────────────────────


def test_parquet_loader_unavailable_emits_info_when_pyarrow_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force pyarrow unavailable; the parquet path emits one info
    finding instead of crashing."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("pyarrow"):
            raise ImportError("pyarrow not installed for this test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    p = tmp_path / "data.parquet"
    p.write_bytes(b"\x00\x00")  # sentinel, never read because we monkeypatched the import
    out = validate_input_dataset(
        tmp_path,
        spec=ValidateInputDatasetSpec(dataset_path=str(p), loader="parquet", row_indices=[0]),
    )
    finding = next(f for f in out.findings if f.code == "parquet_loader_unavailable")
    assert finding.severity == "info"


def test_parquet_clean_pass(tmp_path: Path) -> None:
    """Smoke test for the pyarrow path. Only runs when pyarrow is
    actually installed in the test env."""
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")

    table = pa.table({"target": [1.0, 2.0, 3.0], "x": ["a", "b", "c"]})
    p = tmp_path / "data.parquet"
    pq.write_table(table, p)

    out = validate_input_dataset(
        tmp_path,
        spec=ValidateInputDatasetSpec(
            dataset_path=str(p),
            loader="parquet",
            row_indices=[0, 1, 2],
            required_non_null_cols=["target"],
        ),
    )
    assert out.findings == []
