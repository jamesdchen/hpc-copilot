"""``validate-input-dataset`` primitive — verify dataset row references.

Catches the NaN-trap row-count bug class: tasks.py references row N
which exists in the parquet but is NaN at the columns the executor
reads. The task survives qsub but crashes when the executor tries
to read the value.

Generic over loader (parquet via pyarrow, csv via stdlib, jsonl via
stdlib). Pyarrow is an optional dep; when absent for a parquet path
the validator emits an info-level finding rather than failing — the
rest of the campaign can still validate. ``wc -l`` is never used —
it counts newlines, not records, so it lies on multi-line CSV cells
and JSONL with embedded newlines.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from hpc_agent._internal.primitive import primitive
from hpc_agent._schema_models.validators.validate_input_dataset import (
    ValidateInputDatasetResult,
    ValidateInputDatasetSpec,
)
from hpc_agent._schema_models.workflows.validate_campaign import ValidatorFinding

_VALIDATOR = "validate-input-dataset"


def _is_nullish(value: Any) -> bool:
    """Treat None / NaN / empty-string as null. ``float('nan') != itself``
    is the standard NaN sentinel."""
    if value is None:
        return True
    if isinstance(value, float) and value != value:  # NaN check
        return True
    return isinstance(value, str) and value == ""


def _check_indices(
    n_rows: int,
    row_indices: list[int],
) -> list[ValidatorFinding]:
    findings: list[ValidatorFinding] = []
    for idx in row_indices:
        if idx < 0 or idx >= n_rows:
            findings.append(
                ValidatorFinding(
                    validator=_VALIDATOR,
                    severity="error",
                    code="row_index_oob",
                    message=(
                        f"tasks.py references row index {idx} but dataset has "
                        f"{n_rows} rows (valid range: [0, {n_rows - 1}])"
                    ),
                    suggested_fix=(
                        f"Either remove row {idx} from tasks.py or extend the input dataset."
                    ),
                    evidence={"row_index": idx, "n_rows": n_rows},
                )
            )
    return findings


def _check_non_null_parquet(
    path: Path,
    row_indices: list[int],
    cols: list[str],
) -> list[ValidatorFinding]:
    """Read parquet via pyarrow (optional dep). Returns findings; on
    pyarrow-missing returns one info finding."""
    try:
        import pyarrow.parquet as pq  # noqa: PLC0415 — optional dep
    except ImportError:
        return [
            ValidatorFinding(
                validator=_VALIDATOR,
                severity="info",
                code="parquet_loader_unavailable",
                message="pyarrow not installed; parquet validation skipped.",
                suggested_fix="pip install pyarrow",
                evidence={"path": str(path)},
            )
        ]
    findings: list[ValidatorFinding] = []
    table = pq.read_table(path, columns=cols) if cols else pq.read_table(path)
    n_rows = table.num_rows
    findings.extend(_check_indices(n_rows, row_indices))
    if not cols:
        return findings
    # Pull just the requested rows; pyarrow's take is O(rows-touched).
    in_range = [i for i in row_indices if 0 <= i < n_rows]
    if not in_range:
        return findings
    sub = table.take(in_range).to_pylist()
    for evidence_idx, row in zip(in_range, sub, strict=True):
        for col in cols:
            if _is_nullish(row.get(col)):
                findings.append(
                    ValidatorFinding(
                        validator=_VALIDATOR,
                        severity="error",
                        code="required_column_null",
                        message=(
                            f"row {evidence_idx} has null/NaN at column {col!r}, "
                            f"which the campaign declared as required-non-null"
                        ),
                        suggested_fix=(
                            f"Either skip row {evidence_idx} in tasks.py or "
                            f"backfill column {col!r}."
                        ),
                        evidence={"row_index": evidence_idx, "column": col},
                    )
                )
    return findings


def _load_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def _check_non_null_csv(
    path: Path,
    row_indices: list[int],
    cols: list[str],
) -> list[ValidatorFinding]:
    rows = _load_csv_rows(path)
    findings = _check_indices(len(rows), row_indices)
    if not cols:
        return findings
    for idx in row_indices:
        if not (0 <= idx < len(rows)):
            continue  # already flagged by index check
        row = rows[idx]
        for col in cols:
            if col not in row or _is_nullish(row[col]):
                findings.append(
                    ValidatorFinding(
                        validator=_VALIDATOR,
                        severity="error",
                        code="required_column_null",
                        message=(
                            f"row {idx} has null/missing at column {col!r}, "
                            f"which the campaign declared as required-non-null"
                        ),
                        suggested_fix=(
                            f"Either skip row {idx} in tasks.py or backfill column {col!r}."
                        ),
                        evidence={"row_index": idx, "column": col},
                    )
                )
    return findings


def _load_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with path.open() as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            out.append(json.loads(stripped))
    return out


def _check_non_null_jsonl(
    path: Path,
    row_indices: list[int],
    cols: list[str],
) -> list[ValidatorFinding]:
    rows = _load_jsonl_rows(path)
    findings = _check_indices(len(rows), row_indices)
    if not cols:
        return findings
    for idx in row_indices:
        if not (0 <= idx < len(rows)):
            continue
        row = rows[idx]
        for col in cols:
            if col not in row or _is_nullish(row[col]):
                findings.append(
                    ValidatorFinding(
                        validator=_VALIDATOR,
                        severity="error",
                        code="required_column_null",
                        message=(
                            f"row {idx} has null/missing at field {col!r}, "
                            f"which the campaign declared as required-non-null"
                        ),
                        suggested_fix=(
                            f"Either skip row {idx} in tasks.py or backfill field {col!r}."
                        ),
                        evidence={"row_index": idx, "column": col},
                    )
                )
    return findings


@primitive(
    name=_VALIDATOR,
    verb="validate",
    side_effects=[],
    idempotent=True,
    agent_facing=True,
)
def validate_input_dataset(
    experiment_dir: Path,
    *,
    spec: ValidateInputDatasetSpec,
) -> ValidateInputDatasetResult:
    """Verify ``tasks.py`` row references against the input dataset.

    Each row index in ``spec.row_indices`` must be within bounds and
    every column in ``spec.required_non_null_cols`` must be non-null
    at those rows. Findings are agent-actionable.

    Common ``code`` values:

    * ``dataset_missing`` — file not on disk.
    * ``dataset_unsupported_loader`` — loader name not in
      {parquet, csv, jsonl}.
    * ``dataset_load_error`` — loader raised on parse.
    * ``parquet_loader_unavailable`` — pyarrow optional dep missing
      (info-level, validation skipped).
    * ``row_index_oob`` — index out of bounds.
    * ``required_column_null`` — the NaN-trap bug class.
    """
    path = Path(spec.dataset_path)
    if not path.is_absolute():
        path = experiment_dir / path

    if not path.is_file():
        return ValidateInputDatasetResult(
            findings=[
                ValidatorFinding(
                    validator=_VALIDATOR,
                    severity="error",
                    code="dataset_missing",
                    message=f"input dataset not found at {path}",
                    suggested_fix=(
                        "Verify spec.dataset_path is correct and the file is "
                        "rsynced into the experiment directory."
                    ),
                    evidence={"path": str(path)},
                )
            ]
        )

    try:
        if spec.loader == "parquet":
            findings = _check_non_null_parquet(path, spec.row_indices, spec.required_non_null_cols)
        elif spec.loader == "csv":
            findings = _check_non_null_csv(path, spec.row_indices, spec.required_non_null_cols)
        elif spec.loader == "jsonl":
            findings = _check_non_null_jsonl(path, spec.row_indices, spec.required_non_null_cols)
        else:  # pragma: no cover — Pydantic Literal already gates this.
            findings = [
                ValidatorFinding(
                    validator=_VALIDATOR,
                    severity="error",
                    code="dataset_unsupported_loader",
                    message=f"unsupported loader: {spec.loader!r}",
                )
            ]
    except Exception as exc:  # noqa: BLE001 — pyarrow.lib.ArrowInvalid and friends
        # We deliberately catch broadly: pyarrow may raise ArrowInvalid /
        # ArrowIOError (subclasses of Exception, not OSError/ValueError),
        # the csv/json paths can raise their own Errors, and we never want
        # the validator itself to crash — its contract is "return findings".
        findings = [
            ValidatorFinding(
                validator=_VALIDATOR,
                severity="error",
                code="dataset_load_error",
                message=f"failed to load dataset {path}: {exc}",
                evidence={"path": str(path), "loader": spec.loader},
            )
        ]
    return ValidateInputDatasetResult(findings=findings)
