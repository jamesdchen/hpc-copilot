"""Content manifest + verify-against-manifest — the data-identity artifact (#232).

Today's transfer is *fire-and-forget*: ``rsync_push``/``rsync_pull`` run
inline in submit-flow / aggregate-flow and verify the **exit code only**, so
a truncated or corrupt "completed" transfer is not discovered until
aggregation reduces over it. This module is the irreducible piece #232 says
to own: a manifest (content hash + size + path list) and a verify that checks
**content**, not size-or-existence.

The manifest is a *third* identity alongside the two that already exist:

* ``state/run_sha.py:compute_cmd_sha``      — PARAMETER identity (task kwargs)
* ``state/run_sha.py:compute_tasks_py_sha`` — CODE identity (executor body)
* :meth:`Manifest.digest`                   — **DATA identity** (file content)

so a stage-in can skip when the data is already present by content (the same
dedup discipline ``cmd_sha`` applies to params, applied to files), and a
stage-out can prove the bytes landed intact. This core is
**profile-independent** — worth building under any of #232's data profiles;
the per-profile bracket (shared dataset vs per-task shards vs stage-out-heavy)
is the part still waiting on the profile.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

    from hpc_agent._wire.fixtures.failure_features import FailureFeatures

__all__ = [
    "FileEntry",
    "Manifest",
    "ManifestDelta",
    "VerifyReport",
    "build_manifest",
    "manifest_delta",
    "verify_manifest",
]

_CHUNK = 1024 * 1024  # 1 MiB streaming read — bounded memory on large files.


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class FileEntry:
    """One file's identity within a manifest: relative path + size + content hash."""

    path: str  # POSIX relative path under the manifest root
    size: int
    sha256: str


@dataclass(frozen=True)
class Manifest:
    """A content manifest over a set of files — the data-identity artifact.

    ``entries`` are sorted by path so :meth:`digest` is order-independent and
    the manifest round-trips deterministically.
    """

    entries: tuple[FileEntry, ...]

    @property
    def digest(self) -> str:
        """sha256 over the (path, size, sha256) tuples — the data-identity sha.

        Two trees with identical file content (same paths, sizes, hashes)
        produce the same digest, so a stage-in can dedup on it exactly the way
        ``find_run_by_cmd_sha`` dedups on the parameter sha.
        """
        h = hashlib.sha256()
        for e in self.entries:
            h.update(f"{e.path}\0{e.size}\0{e.sha256}\n".encode())
        return h.hexdigest()

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(e.path for e in self.entries)

    def to_dict(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "files": [{"path": e.path, "size": e.size, "sha256": e.sha256} for e in self.entries],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Manifest:
        entries = tuple(
            FileEntry(path=str(f["path"]), size=int(f["size"]), sha256=str(f["sha256"]))
            for f in payload.get("files", [])
        )
        return cls(entries=tuple(sorted(entries, key=lambda e: e.path)))


@dataclass(frozen=True)
class ManifestDelta:
    """The ADDITIVE delta from a REMOTE manifest to a LOCAL one.

    Answers "what must a transfer ship so the remote's content matches local,
    WITHOUT deleting anything the remote already has". This is the atom the
    rsync-less deploy delta (queue item 6b) stands on: when no ``rsync`` is on
    PATH the tar fallback would re-ship the whole tree, so instead it hashes
    both trees (the same content-hash identity :class:`Manifest` computes),
    diffs them here, and tars only :attr:`to_ship`.

    * ``missing``    — paths present locally, ABSENT on the remote (never shipped)
    * ``mismatched`` — paths on BOTH sides whose content sha256 differs
    * ``extra``      — paths on the remote, absent locally. Reported for
      disclosure only and **never deleted** — deletion is out of scope for the
      rsync-less delta (an rsync ``--delete`` is the tool for that). Kept
      distinct so a caller can surface it without acting on it.

    Comparison is by content sha256 (size is redundant once the hash matches),
    so two byte-identical files at the same path are never re-shipped.
    """

    missing: tuple[str, ...]
    mismatched: tuple[str, ...]
    extra: tuple[str, ...]

    @property
    def to_ship(self) -> tuple[str, ...]:
        """The exact set a delta transfer tars — ``missing + mismatched``, sorted.

        Excludes ``extra`` (additive-only: the remote keeps files local dropped).
        """
        return tuple(sorted((*self.missing, *self.mismatched)))

    @property
    def nothing_to_ship(self) -> bool:
        """True when the remote is already content-identical for every local file."""
        return not self.missing and not self.mismatched


def manifest_delta(local: Manifest, remote: Manifest) -> ManifestDelta:
    """Diff a LOCAL manifest against a REMOTE one — the additive transfer set.

    Pure function over two manifests. A local path absent on the remote is
    ``missing``; present on both with a differing sha256 is ``mismatched``;
    present on the remote but not locally is ``extra`` (reported, never acted
    on). :attr:`ManifestDelta.to_ship` is ``missing + mismatched`` — exactly the
    files a delta tar must carry so the remote's content matches local.
    """
    remote_sha = {e.path: e.sha256 for e in remote.entries}
    local_paths = {e.path for e in local.entries}
    missing: list[str] = []
    mismatched: list[str] = []
    for e in local.entries:
        rsha = remote_sha.get(e.path)
        if rsha is None:
            missing.append(e.path)
        elif rsha != e.sha256:
            mismatched.append(e.path)
    extra = [p for p in remote_sha if p not in local_paths]
    return ManifestDelta(
        missing=tuple(sorted(missing)),
        mismatched=tuple(sorted(mismatched)),
        extra=tuple(sorted(extra)),
    )


def build_manifest(root: Path, *, paths: Iterable[str] | None = None) -> Manifest:
    """Build a :class:`Manifest` for *root*.

    When *paths* is given, manifests exactly those relative paths (the
    stage spec's declared file set); otherwise walks every file under *root*.
    Each entry carries the file's size and streamed sha256. Raises
    ``FileNotFoundError`` if a declared path is absent — building a manifest
    of a set you can't read is a hard error, not a silent omission.
    """
    root = Path(root)
    if paths is not None:
        rels = [str(p) for p in paths]
    else:
        rels = [
            os.path.relpath(os.path.join(dirpath, name), root)
            for dirpath, _dirs, files in os.walk(root)
            for name in files
        ]
    entries: list[FileEntry] = []
    for rel in rels:
        rel_posix = Path(rel).as_posix()
        full = root / rel
        if not full.is_file():
            raise FileNotFoundError(f"manifest path not found under {root}: {rel}")
        entries.append(FileEntry(path=rel_posix, size=full.stat().st_size, sha256=_sha256_of(full)))
    entries.sort(key=lambda e: e.path)
    return Manifest(entries=tuple(entries))


@dataclass(frozen=True)
class VerifyReport:
    """The result of verifying a tree against a manifest.

    ``ok`` is True only when every manifested file is present with a matching
    size AND content hash. The three failure buckets are kept distinct because
    they route differently in #232's taxonomy: ``missing`` is structural
    (fail-fast / escalate — quota, permission, an absent output), while
    ``size_mismatch`` / ``hash_mismatch`` are corrupt/partial (a truncated
    transfer to resume or re-pull).
    """

    ok: bool
    checked: int
    missing: tuple[str, ...] = ()
    size_mismatch: tuple[str, ...] = ()
    hash_mismatch: tuple[str, ...] = ()

    def failure_features(self) -> FailureFeatures:
        """Project a failed verification into a #230 evidence vector, so a bad
        transfer escalates through the same decision path as any other failure
        (e.g. a stage-out quota gate → a decide gate, per #231/#232) instead of
        being discovered late at aggregation.
        """
        from hpc_agent._wire.fixtures.failure_features import FailureFeatures

        raw = (
            "outputs_missing"
            if self.missing
            else "corrupt_transfer"
            if (self.hash_mismatch or self.size_mismatch)
            else "verify_ok"
        )
        return FailureFeatures.model_validate(
            {
                "error_class": "unknown",
                "error_class_raw": raw,
                "resource_spec": {
                    "checked": self.checked,
                    "missing": len(self.missing),
                    "size_mismatch": len(self.size_mismatch),
                    "hash_mismatch": len(self.hash_mismatch),
                },
            }
        )


def verify_manifest(root: Path, manifest: Manifest, *, check_hash: bool = True) -> VerifyReport:
    """Verify *root* against *manifest* by CONTENT, not size-or-existence.

    For each manifested file: confirm it exists, its size matches, and (unless
    *check_hash* is False) its streamed sha256 matches. *check_hash=False* is a
    deliberate escape hatch for stage-out-heavy profiles where re-hashing huge
    results is the bottleneck — size+existence is weaker but cheap; the default
    is the full content check because the truncated-but-complete-looking
    transfer is exactly the silent failure this exists to catch.
    """
    root = Path(root)
    missing: list[str] = []
    size_mismatch: list[str] = []
    hash_mismatch: list[str] = []
    for e in manifest.entries:
        full = root / e.path
        if not full.is_file():
            missing.append(e.path)
            continue
        if full.stat().st_size != e.size:
            size_mismatch.append(e.path)
            continue
        if check_hash and _sha256_of(full) != e.sha256:
            hash_mismatch.append(e.path)
    ok = not (missing or size_mismatch or hash_mismatch)
    return VerifyReport(
        ok=ok,
        checked=len(manifest.entries),
        missing=tuple(missing),
        size_mismatch=tuple(size_mismatch),
        hash_mismatch=tuple(hash_mismatch),
    )
