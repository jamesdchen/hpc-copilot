"""Bounded auto-prune of MANIFEST-KNOWN remote extras (data-manifest ruling 6).

The rsync-less delta push (:func:`hpc_agent.infra.transport.rsync_push`) is
additive: it ships ``missing + mismatched`` and, historically, NEVER pruned the
remote's ``extra`` (files present remotely, absent locally). That leaves stale
framework/code files on the cluster forever on hosts without ``rsync --delete``.

The ruling (``docs/design/data-manifest.md`` foot, 2026-07-10) narrows what may
be auto-deleted to the only class the framework can prove is *ours*:

* **manifest-known** — a remote extra whose path is recorded in the PRIOR
  deploy's push manifest (we shipped it before; it has since dropped from the
  deploy set). Prunable, under a disclosed bound.
* **anomaly** — a remote extra NOT in the prior push manifest. Something we
  never shipped (a user's stray output outside the excluded run dirs, a foreign
  file). NEVER deleted — surfaced to ask.

The bound is a conservative twin cap (file count + total bytes). When the
manifest-known set exceeds *either* cap the plan is REFUSED wholesale — nothing
is pruned — and the refusal is disclosed. A bounded auto-delete never becomes an
unbounded one; past the bound a human decides.

This module is the PURE planner (identity / comparison / counting over opaque
paths — the agnostic core surface, no ssh, no I/O). The transport layer feeds it
the delta's ``extra`` entries and the prior push manifest's path set, then
journals + executes exactly :attr:`PrunePlan.to_prune`.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass

from hpc_agent.ops.transfer.manifest import FileEntry

__all__ = [
    "DEFAULT_PRUNE_MAX_BYTES",
    "DEFAULT_PRUNE_MAX_FILES",
    "PrunePlan",
    "plan_prune",
]

#: Conservative default caps on an auto-prune. A deploy tree that legitimately
#: dropped more than 100 files (or 100 MiB) of manifest-known content in ONE
#: push is a large enough change to warrant a human look — so the auto-prune
#: refuses and discloses rather than silently deleting at scale. Overridable at
#: the transport call site (env: ``HPC_DEPLOY_PRUNE_MAX_FILES`` /
#: ``HPC_DEPLOY_PRUNE_MAX_BYTES``).
DEFAULT_PRUNE_MAX_FILES: int = 100
DEFAULT_PRUNE_MAX_BYTES: int = 100 * 1024 * 1024  # 100 MiB


@dataclass(frozen=True)
class PrunePlan:
    """The vetted outcome of planning a bounded auto-prune.

    * ``prunable`` — the manifest-known extras (each a :class:`FileEntry` carrying
      the remote path + size + the *old* remote sha, for the journal). Deleted
      only when ``not refused``.
    * ``anomalies`` — remote extras that are NOT manifest-known. NEVER deleted;
      surfaced so a human decides. Sorted paths.
    * ``refused`` — True when the manifest-known set breaches a cap; then nothing
      is pruned (``to_prune`` is empty) and ``refuse_reason`` names which cap.
    * ``prune_bytes`` — total bytes of ``prunable`` (the would-be delete size,
      whether or not refused).
    """

    prunable: tuple[FileEntry, ...]
    anomalies: tuple[str, ...]
    refused: bool
    refuse_reason: str | None
    prune_bytes: int
    max_files: int
    max_bytes: int

    @property
    def to_prune(self) -> tuple[str, ...]:
        """The exact remote paths to delete — empty when the plan is refused."""
        if self.refused:
            return ()
        return tuple(e.path for e in self.prunable)


def plan_prune(
    extra_entries: Iterable[FileEntry],
    manifest_known: Collection[str],
    *,
    max_files: int = DEFAULT_PRUNE_MAX_FILES,
    max_bytes: int = DEFAULT_PRUNE_MAX_BYTES,
) -> PrunePlan:
    """Split remote extras into prunable (manifest-known) vs anomalies, under a bound.

    *extra_entries* are the remote-only files (from the manifest delta's
    ``extra``, resolved to their remote :class:`FileEntry` so size + old sha
    ride along). *manifest_known* is the set of paths the PRIOR push manifest
    recorded as shipped by us.

    A remote extra whose path is in *manifest_known* is prunable; every other
    extra is an ANOMALY (never deleted). If the prunable set exceeds *max_files*
    OR its total size exceeds *max_bytes*, the whole plan is REFUSED — no partial
    auto-delete — with a reason naming the breached cap.
    """
    known = frozenset(manifest_known)
    prunable: list[FileEntry] = []
    anomalies: list[str] = []
    for e in sorted(extra_entries, key=lambda e: e.path):
        if e.path in known:
            prunable.append(e)
        else:
            anomalies.append(e.path)

    prune_bytes = sum(e.size for e in prunable)
    refused = False
    reason: str | None = None
    if len(prunable) > max_files:
        refused = True
        reason = (
            f"{len(prunable)} manifest-known extras exceed the max-files cap "
            f"({max_files}); refusing to auto-prune — a human should review."
        )
    elif prune_bytes > max_bytes:
        refused = True
        reason = (
            f"{prune_bytes} bytes of manifest-known extras exceed the max-bytes cap "
            f"({max_bytes}); refusing to auto-prune — a human should review."
        )

    return PrunePlan(
        prunable=tuple(prunable),
        anomalies=tuple(anomalies),
        refused=refused,
        refuse_reason=reason,
        prune_bytes=prune_bytes,
        max_files=max_files,
        max_bytes=max_bytes,
    )
