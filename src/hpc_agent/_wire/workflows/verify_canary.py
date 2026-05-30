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
    # Every status poll failed — broken cluster-side reporter (the job may have
    # run but its result can't be read, so the canary can't be trusted).
    "reporter_unreachable",
    # The job left the scheduler queue without recording a completion and no
    # stderr marker explains why — resolved fast instead of riding the full
    # wait budget (#193).
    "completed_unknown",
    "timeout",
    "abandoned",
]


class VerifyCanaryResult(BaseModel):
    """Result of the wait + grep + output-check protocol for a 1-task canary.

    Caller branches on ``ok``: True → main array submit; False →
    surface stderr_tail to the user verbatim.
    """

    model_config = ConfigDict(extra="forbid", title="verify-canary output")

    ok: bool
    failure_kind: CanaryFailureKind | None = None
    details: str = Field(
        description="One-line human-readable summary the slash command surfaces above the raw stderr_tail.",
    )
    stderr_tail: str = Field(
        description="Last ~50 lines of the canary's stderr log (or empty string when not retrievable).",
    )
    metrics_fingerprint: str | None = Field(
        default=None,
        description=(
            "Optional sha256 of the canary's expected output file when "
            "the caller asked for a fingerprint. None on every failure "
            "path and when fingerprinting is skipped or fails."
        ),
    )
