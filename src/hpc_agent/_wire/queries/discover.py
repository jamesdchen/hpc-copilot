"""Pydantic model for the merged ``discover`` query verb's output.

One verb, three kinds (``--kind executors|runs|reducers``). The result
carries ``kind`` plus exactly the matching key — each kind keeps the
result key its pre-merge verb emitted (``executors`` / ``runs`` /
``reducers``), so a consumer reading ``data.runs`` is unaffected by the
verb consolidation.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class _ExecutorEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    path: str
    cli_framework: str | None = None
    has_main_guard: bool


class _RunEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    path: str
    gpu: bool
    run_signature_sha: str
    flags: list[str]


class _ReducerEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    path: str
    matches: list[str]
    docstring: str | None = None


class DiscoverResult(BaseModel):
    model_config = ConfigDict(extra="forbid", title="discover output data")

    kind: Literal["executors", "runs", "reducers"]
    executors: list[_ExecutorEntry] | None = None
    runs: list[_RunEntry] | None = None
    reducers: list[_ReducerEntry] | None = None
