"""Pydantic model for the ``check-preflight`` validator's output."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class _PreflightCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    ok: bool
    detail: str | None = None


class PreflightResult(BaseModel):
    """Shape of the ``data`` field on a successful ``preflight`` envelope."""

    model_config = ConfigDict(extra="forbid", title="preflight output data")

    all_ok: bool
    checks: list[_PreflightCheck]
