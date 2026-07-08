"""Pydantic models for the ``pack-bind`` mutate verb (domain-packs T3/T4).

Wire surface over :mod:`hpc_agent.ops.pack.bind_op` — the bind event
(``docs/design/domain-packs.md``, "The bind event"). Binding enters pack
content into an experiment AS DATA (DP1): a caller-referenced manifest relpath
core reads ON DISK, whose every listed file it re-hashes (raw-bytes SHA-256),
refusing on any mismatch, then journals as a CODE attestation under the
dedicated ``"pack"`` scope kind.

**The spec is minimal by construction.** The verb recomputes EVERYTHING
server-side from the manifest on disk — name, version, file shas, seams — so
the caller supplies only the manifest relpath (and, optionally, the pack name
it *expects*, as a cross-check the verb rejects on mismatch; the manifest's own
``name`` remains authoritative). No sha is ever caller-suppliable: a bind can
no more assert a sha into existence than a sign-off can (D5 lock 2). The result
is a pure ECHO of what was bound, for the caller's confirmation and the journal.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import RunIdStrict


class PackBindSpec(BaseModel):
    """Inputs to ``pack-bind`` — a single caller-referenced manifest relpath.

    ``manifest`` is an experiment-dir-relative path to the pack manifest ``.py``
    /``.json`` (resolved exactly as ``_AuditedSource.source`` resolves — core
    never asks how the bytes got there, DP3). The verb reads it ON DISK and
    recomputes every file sha, the manifest sha, name, version, and seams; none
    of those are caller-suppliable.

    ``pack`` is an OPTIONAL cross-check: when present, the verb refuses if the
    manifest's own ``name`` does not equal it (a caller-side guard against
    binding the wrong manifest). Absent → the manifest's ``name`` is taken as
    authoritative with no cross-check.
    """

    model_config = ConfigDict(extra="forbid", title="pack-bind input spec")

    manifest: str = Field(
        min_length=1,
        description=(
            "Experiment-dir-relative path to the pack manifest. Read on disk; the "
            "verb recomputes every listed file's raw-bytes SHA-256, the manifest "
            "sha, and the declared seams server-side (no sha is caller-suppliable)."
        ),
    )
    pack: RunIdStrict | None = Field(
        default=None,
        description=(
            "Optional expected pack name (filesystem-safe slug). When present the "
            "verb refuses if the manifest's own `name` differs — a cross-check "
            "against binding the wrong manifest. Absent → manifest `name` wins."
        ),
    )


class PackFileEntry(BaseModel):
    """One bound file: its manifest-declared relpath and server-recomputed sha.

    ``sha256`` is the raw-bytes SHA-256 the verb recomputed from disk (lowercase
    hex), NEVER a caller-asserted value — the closed integrity set of the bind.
    """

    model_config = ConfigDict(extra="forbid", title="pack-bind file entry")

    path: str = Field(min_length=1, description="Manifest-declared file relpath.")
    sha256: str = Field(
        min_length=1,
        description="Server-recomputed raw-bytes SHA-256 (lowercase hex) of the file on disk.",
    )


class PackBindResult(BaseModel):
    """Echo of what a ``pack-bind`` bound — the journaled bind record's shape.

    Every field is a server-recomputed identity: ``pack``/``version`` from the
    manifest (version echoed, never compared — ORDERING is the sha's job via
    bind order), ``manifest_sha`` the pack identity sha, ``files`` the closed
    integrity set with per-file shas, ``seams`` the declared seam names.
    """

    model_config = ConfigDict(extra="forbid", title="pack-bind output data")

    pack: str
    version: str
    manifest_sha: str
    files: list[PackFileEntry] = Field(
        default_factory=list,
        description="Every bound file with its server-recomputed raw-bytes sha.",
    )
    seams: list[str] = Field(
        default_factory=list,
        description="Declared seam names (drawn from the closed SEAM_NAMES vocabulary).",
    )
