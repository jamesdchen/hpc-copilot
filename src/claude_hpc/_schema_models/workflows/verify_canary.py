"""Pydantic model for the ``verify-canary`` workflow atom's output."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CanaryFailureKind = Literal[
    "dispatcher_failed",
    "import_error",
    "module_not_found",
    "traceback",
    "oom_killed",
    "segfault",
    "missing_output",
    "timeout",
    "abandoned",
]


class VerifyCanaryResult(BaseModel):
    """Result of the wait + grep + output-check protocol for a 1-task canary.

    Caller branches on ``ok``: True → main array submit; False →
    surface stderr_tail to the user verbatim.
    """

    model_config = ConfigDict(title="verify-canary output")

    ok: bool
    failure_kind: CanaryFailureKind | None = None
    details: str = Field(
        description="One-line human-readable summary the slash command surfaces above the raw stderr_tail.",
    )
    stderr_tail: str = Field(
        description="Last ~50 lines of the canary's stderr log (or empty string when not retrievable).",
    )
