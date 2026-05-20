"""Pydantic model for the ``cluster-reduce`` mutator's output."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._schema_models._shared import RunIdLoose


class ClusterReduceResult(BaseModel):
    """Result of running the user's reducer on the cluster + pulling its single JSON output.

    The ``reduced`` field is the parsed JSON the reducer wrote,
    surfaced inline so the agent doesn't need to re-read the local
    file.
    """

    model_config = ConfigDict(extra="forbid", title="cluster-reduce output")

    ok: bool
    run_id: RunIdLoose = Field(
        description="Run identifier — typically YYYYMMDD-HHMMSS-<short_sha>. Loose validation (bare ``str``); the strict-pattern check belongs on input specs, not on outputs that may surface legacy or migrated sidecars.",
    )
    output_path_remote: str = Field(
        description="Path on the cluster where the reducer wrote its output (relative to remote_path or absolute).",
    )
    output_path_local: str = Field(
        description="Local path the reducer's output was rsync_pulled to."
    )
    reduced: Any = Field(
        description="Parsed reducer output (whatever JSON shape the reducer wrote)."
    )
    exit_code: int = Field(ge=0)
    stderr_tail: str = Field(description="Last ~2KB of the reducer's stderr.")
