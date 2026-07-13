"""Pydantic models for the ``dir-digest`` query verb (run-#11 mechanization).

Wire surface over :mod:`hpc_agent.ops.dir_digest` — a BOUNDED, code-rendered
digest of a directory tree. ``dir-digest`` exists to REPLACE unbounded ``ls`` /
``find`` in agent prose: instead of shipping a listing whose size scales with
the tree (a context-budget hazard the moment a results dir holds thousands of
shards), it computes fixed-size NUMBERS server-side (locally, or over ONE
throttled ssh read when ``cluster`` is set) — file/dir counts, total size, the
newest N entries, an extension histogram capped at the top ~10, and (opt-in)
counts of the engine's own failure markers across ``*.log`` / ``*.err`` files.

Boundedness is the contract: for a 1000-file tree the result carries a handful
of scalars, ≤ ``newest`` entries, ≤ 10 histogram buckets, and ≤ len(markers)
counts — never a per-file listing. That is what makes it safe to relay.

Marker vocabulary is REUSED from
:data:`hpc_agent.ops.worker_log_digest.KNOWN_MARKERS` (the one-definition rule:
the engine's own bracket vocabulary lives once, in the worker-log digest, and
this verb imports it rather than re-listing it).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DirEntry(BaseModel):
    """One tree entry in the bounded ``newest`` list."""

    model_config = ConfigDict(extra="forbid")

    relpath: str = Field(description="Path relative to the digested root.")
    size: int = Field(description="Size in bytes (st_size; for a dir, the entry's own size).")
    mtime: float = Field(description="Modification time as a POSIX timestamp (epoch seconds).")


class HistogramBucket(BaseModel):
    """One extension / name-group bucket, count-descending in the result."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        description="The group key — a lowercased file extension incl. the dot, or '(noext)'."
    )
    count: int = Field(description="Number of files in this group.")


class DirDigestSpec(BaseModel):
    """Inputs to ``dir-digest``."""

    model_config = ConfigDict(extra="forbid", title="dir-digest input spec")

    path: str = Field(
        min_length=1,
        description=(
            "Directory to digest. LOCAL (no --cluster): a path relative to "
            "--experiment-dir, or an absolute path that resolves WITHIN it. "
            "REMOTE (--cluster set): an absolute cluster path that must resolve "
            "strictly under the cluster's scratch root (the same confinement "
            "inspect-deployment uses)."
        ),
    )
    newest: int = Field(
        default=10,
        ge=0,
        description=(
            "How many newest-by-mtime entries to include (default 10). 0 omits "
            "the list; the counts/size/histogram still compute."
        ),
    )
    marker_scan: bool = Field(
        default=True,
        description=(
            "When true (default), scan *.log/*.err files for the engine's known "
            "failure markers and report per-marker line-hit counts (bounded "
            "per-file). False skips the scan entirely."
        ),
    )
    cluster: str | None = Field(
        default=None,
        description=(
            "Cluster key from clusters.yaml to digest a REMOTE tree over one "
            "throttled ssh read. Omit for a LOCAL digest (the first-class path)."
        ),
    )


class DirDigestResult(BaseModel):
    """The bounded, code-rendered digest of one directory tree.

    On a missing/unreadable root the verb FAILS OPEN: ``readable`` is False,
    ``error`` names the problem, the numbers are zero/empty, and ``render``
    states it plainly — never a traceback.
    """

    model_config = ConfigDict(extra="forbid", title="dir-digest output data")

    path: str = Field(description="The resolved root that was digested (absolute).")
    cluster: str | None = Field(
        default=None, description="The cluster digested over ssh, or null for a local digest."
    )
    scope: Literal["local", "remote"] = Field(description="Whether the digest was local or remote.")
    exists: bool = Field(description="Whether the root exists.")
    readable: bool = Field(description="Whether the root could be read/probed.")
    error: str | None = Field(
        default=None,
        description="Fail-open diagnostic when the root is missing/unreadable; else null.",
    )
    file_count: int = Field(default=0, description="Number of regular files under the root.")
    dir_count: int = Field(default=0, description="Number of subdirectories under the root.")
    total_size_bytes: int = Field(
        default=0, description="Sum of regular-file sizes in bytes under the root."
    )
    newest_requested: int = Field(
        description="The `newest` count the caller asked for (echoed for provenance)."
    )
    newest: list[DirEntry] = Field(
        default_factory=list,
        description="Up to `newest_requested` entries, newest-mtime first. Always bounded.",
    )
    histogram: list[HistogramBucket] = Field(
        default_factory=list,
        description="Top ~10 extension/name-group buckets, count-descending. Always bounded.",
    )
    marker_scan: bool = Field(description="Whether a marker scan was requested.")
    marker_counts: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Per-marker count of lines CONTAINING each known engine marker across "
            "*.log/*.err files. Every known marker is present (0 when absent) when "
            "marker_scan is true; empty when marker_scan is false or unreadable."
        ),
    )
    files_scanned_for_markers: int = Field(
        default=0,
        description="How many *.log/*.err files the (bounded) marker scan actually read.",
    )
    render: str = Field(
        description=(
            "Deterministic markdown digest (bounded numbers only) the caller "
            "relays VERBATIM — never re-interpreted into freeform prose."
        ),
    )
