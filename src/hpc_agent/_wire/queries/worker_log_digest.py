"""Pydantic models for the ``worker-log-digest`` query verb (run-#10 finding G2).

Wire surface over :mod:`hpc_agent.ops.worker_log_digest` — a code-rendered,
deterministic digest of a local worker log. ``worker-log-digest`` is a PURE READ
of a LOCAL file (no SSH): it counts the lines carrying each KNOWN engine marker,
reports the total line count, echoes the last N lines VERBATIM, and renders a
markdown projection the caller relays without interpreting.

Why it exists: the premortem instructed the LLM to open raw worker logs and eye
them for ``[throttle]`` / ``[fatal]`` markers — an unmechanized reading of
untrusted log text, the run-#9 judgment-in-prose strike class. This verb turns
that scan into code: the marker vocabulary is derived from what the engine
actually emits (see :data:`hpc_agent.ops.worker_log_digest.KNOWN_MARKERS`), and
the counts + tail are computed, never divined.

Boundary posture: raw log text is UNTRUSTED DATA. The digest only counts fixed
marker substrings and echoes bytes verbatim — it attaches no meaning to a line
and makes no verdict about what the run did.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class WorkerLogDigestSpec(BaseModel):
    """Inputs to ``worker-log-digest``."""

    model_config = ConfigDict(extra="forbid", title="worker-log-digest input spec")

    log_path: str = Field(
        min_length=1,
        description=(
            "Path to the worker log to digest — a path relative to "
            "--experiment-dir, or an absolute path that resolves WITHIN the "
            "experiment dir (detached-worker logs land under .hpc/_detached/). "
            "No SSH: a local file only."
        ),
    )
    tail_lines: int = Field(
        default=50,
        ge=0,
        description=(
            "How many trailing lines to echo verbatim in the digest (default 50). "
            "0 echoes none — the marker counts and total still compute."
        ),
    )


class WorkerLogDigestResult(BaseModel):
    """The code-rendered digest of one worker log.

    On an unreadable/missing file the verb FAILS OPEN: ``readable`` is False,
    ``error`` names the problem, the counts/tail are empty, and ``render``
    states it plainly — never a traceback.
    """

    model_config = ConfigDict(extra="forbid", title="worker-log-digest output data")

    log_path: str = Field(description="The resolved absolute path that was digested.")
    exists: bool = Field(description="Whether the log file exists on disk.")
    readable: bool = Field(description="Whether the log file could be read.")
    error: str | None = Field(
        default=None,
        description="Fail-open diagnostic when the file is missing/unreadable; else null.",
    )
    total_lines: int = Field(
        default=0,
        description="Total line count of the log (0 when unreadable).",
    )
    tail_lines_requested: int = Field(
        description="The tail_lines the caller asked for (echoed back for provenance).",
    )
    marker_counts: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Per-marker count of lines CONTAINING that known engine marker, keyed "
            "by the exact marker string. Empty when unreadable. Every known marker "
            "is present (0 when absent) so the shape is stable."
        ),
    )
    tail: list[str] = Field(
        default_factory=list,
        description="The last tail_lines lines of the log, VERBATIM (newlines stripped).",
    )
    render: str = Field(
        description=(
            "Deterministic markdown digest (counts + verbatim fenced tail) the "
            "caller relays VERBATIM — never re-interpreted into freeform prose."
        ),
    )
