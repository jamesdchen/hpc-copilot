"""Wire model for the ``validate-input-dataset`` atom.

Catches the NaN-trap row-count bug class: ``tasks.py`` references
row index N that exists in the parquet but is NaN at the columns
the executor reads, so the task starts and crashes later. The fix
is to verify, BEFORE submission, that every referenced row exists
and the required columns are non-null.

Generic over loader (parquet via pyarrow / csv via stdlib /
jsonl via stdlib). Pyarrow is an optional dep; when absent for a
parquet path the validator emits an ``info`` finding rather than
failing — the rest of the campaign can still validate.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire.workflows.validate_campaign import (
    ValidatorFinding,  # noqa: TC001 — Pydantic resolves the annotation at runtime
)


class ValidateInputDatasetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_path: str = Field(min_length=1)
    loader: Literal["parquet", "csv", "jsonl"]
    # Empty list is a deliberate "loader smoke-test" request (open the
    # file but skip row-level checks). The validate-campaign workflow's
    # gate (``dataset_row_indices is not None``) treats it as an
    # explicit "still run the validator" signal, distinct from "absent".
    # Previously this constraint crashed the workflow on empty input
    # with a Pydantic ValidationError instead of running the loader.
    row_indices: list[int] = Field(default_factory=list)
    required_non_null_cols: list[str] = Field(default_factory=list)


class ValidateInputDatasetResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: list[ValidatorFinding] = Field(default_factory=list)
