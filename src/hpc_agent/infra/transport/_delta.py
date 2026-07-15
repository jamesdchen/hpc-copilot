"""Content-hash push delta: local + remote manifests for the rsync-less path.

The tar full-copy fallback has no delta, so it re-ships the whole tree even when
the remote is byte-identical (the run-#11 8.4 GB re-ship). This module builds the
two content manifests the delta diffs: the local one (over the exclude-filtered
push tree) and the remote one (the deployed runtime hashes its own tree in one
bounded ssh round-trip, via :data:`_REMOTE_MANIFEST_SNIPPET`). Both sides use the
same :func:`_path_excluded` file-set test so they describe the same tree.
"""

from __future__ import annotations

import base64
import contextlib
import json
import shlex
import sys
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from ._excludes import _pushable_relpaths

if TYPE_CHECKING:
    from collections.abc import Iterator

#: Cap on the remote hash manifest's file count. A delta needs one sha per
#: file shipped back over the (slow) link; past this the manifest stops being
#: "bounded output" and the push falls back to the full tar (disclosed) rather
#: than pull a pathological payload back. The pushable code/data tree is small
#: (run output dirs are excluded), so a real push never approaches this.
_DELTA_MANIFEST_FILE_CAP: Final[int] = 100_000

#: Env kill-switch: set ``HPC_NO_DEPLOY_DELTA=1`` to force the whole-tree tar
#: copy on rsync-less hosts even when a remote manifest is available (mirrors
#: ``HPC_NO_DEPLOY_CACHE`` for :func:`deploy_runtime`). The full-copy disclosure
#: then names this as the reason.
_DELTA_ENV_KILL = "HPC_NO_DEPLOY_DELTA"

#: The self-contained python the DEPLOYED runtime runs cluster-side to hash its
#: own tree — the "remote side hashes its deployed tree, shipped back as a hash
#: manifest" half of item 6b. Stdlib-only so it runs under any cluster ``python3``
#: without the framework installed; base64-piped over one ssh round-trip so no
#: quoting of the source is needed. It mirrors :func:`_path_excluded` and
#: :class:`Manifest`'s content-hash exactly, so local and remote agree on both
#: the file set and each file's identity. Emits ``{"files": [...]}`` (the
#: :meth:`Manifest.from_dict` shape); prints nothing — routing the caller to the
#: full-copy fallback — on any error, a first/absent tree, or a file count past
#: the cap.
_REMOTE_MANIFEST_SNIPPET = textwrap.dedent(
    """
    import os, sys, json, hashlib, fnmatch
    try:
        pats = [str(p).rstrip('/') for p in json.loads(os.environ.get('HPC_DELTA_EXCLUDES', '[]'))]
        cap = int(os.environ.get('HPC_DELTA_CAP', '100000'))

        def excluded(parts):
            rel = '/'.join(parts)
            for pat in pats:
                if pat.startswith('./') or pat.startswith('^'):
                    a = pat[2:] if pat.startswith('./') else pat[1:]
                    if parts and fnmatch.fnmatch(parts[0], a):
                        return True
                    continue
                if '/' in pat:
                    if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, pat + '/*'):
                        return True
                    continue
                for part in parts:
                    if fnmatch.fnmatch(part, pat):
                        return True
            return False

        files = []
        for dp, dirs, names in os.walk('.'):
            rel = '' if dp == '.' else os.path.relpath(dp, '.').replace(os.sep, '/')
            base = tuple(rel.split('/')) if rel else ()
            dirs[:] = [d for d in dirs if not excluded(base + (d,))]
            for n in names:
                parts = base + (n,)
                if excluded(parts):
                    continue
                full = os.path.join(dp, n)
                if not os.path.isfile(full):
                    continue
                try:
                    h = hashlib.sha256()
                    with open(full, 'rb') as fh:
                        for chunk in iter(lambda: fh.read(1048576), b''):
                            h.update(chunk)
                    size = os.path.getsize(full)
                except OSError:
                    continue
                files.append({'path': '/'.join(parts), 'size': size, 'sha256': h.hexdigest()})
                if len(files) > cap:
                    sys.exit(0)  # too big -> no output -> caller ships the whole tree
        sys.stdout.write(json.dumps({'files': files}))
    except Exception:
        pass
    """
).strip()


#: Local-only quick-check cache for the push-delta content-hash scan (run-13
#: finding 6). Keyed by relpath -> (size, mtime_ns, sha256); a scan reuses the
#: cached sha when (size, mtime_ns) still match, so a re-push re-hashes only the
#: files that actually changed instead of the whole tree (the live 39,374-file /
#: 9.9 GB / ~37-minute cold re-hash). Sibling of :data:`_PUSH_MANIFEST_REL`
#: under ``.hpc/`` — a stack-internal file, unioned into the push exclude set in
#: :func:`rsync_push` so it is never itself hashed, shipped, or pruned (mirrors
#: how ``.hpc/.deploy_state.json`` / ``.hpc/.push_manifest.json`` are excluded).
_PUSH_HASH_CACHE_REL: Final[str] = ".hpc/.push_hash_cache.json"

#: Schema marker on the cache doc; a mismatch (or any corrupt/unreadable cache)
#: is discarded silently and the scan full-re-hashes — fail-open on a pure
#: optimization, never a correctness dependency.
_HASH_CACHE_VERSION: Final[int] = 1

#: Delta ship-batch caps (run-13 finding 3). The delta ships in bounded batches
#: so a died-mid-push retry re-pays only the in-flight batch, not the whole
#: delta; the push manifest is checkpointed after each batch lands. Bounded in
#: BOTH dimensions (a batch of many tiny files vs a few huge files) — whichever
#: cap trips first closes the batch. A single file larger than the byte cap
#: still ships alone (never split — the tar member is atomic). Env-overridable
#: for ops + tests via ``HPC_DELTA_BATCH_MAX_FILES`` / ``HPC_DELTA_BATCH_MAX_BYTES``.
_DELTA_BATCH_MAX_FILES: Final[int] = 2000
_DELTA_BATCH_MAX_BYTES: Final[int] = 256 * 1024 * 1024  # 256 MiB


def _delta_batch_caps() -> tuple[int, int]:
    """The (max_files, max_bytes) delta ship-batch caps, env-overridable."""
    from hpc_agent.infra.remote import _env_int

    return (
        max(1, _env_int("HPC_DELTA_BATCH_MAX_FILES", _DELTA_BATCH_MAX_FILES)),
        max(1, _env_int("HPC_DELTA_BATCH_MAX_BYTES", _DELTA_BATCH_MAX_BYTES)),
    )


def _delta_ship_batches(
    ship: list[str], sizes: dict[str, int], *, max_files: int, max_bytes: int
) -> Iterator[list[str]]:
    """Partition the ordered delta *ship* list into bounded batches.

    A batch closes when adding the next file would exceed EITHER the file-count
    or the byte cap; an oversized single file still forms its own batch (a tar
    member is never split). Pure + deterministic so the checkpoint cadence is
    unit-testable without a transfer.
    """
    batch: list[str] = []
    batch_bytes = 0
    for path in ship:
        size = sizes.get(path, 0)
        if batch and (len(batch) >= max_files or batch_bytes + size > max_bytes):
            yield batch
            batch, batch_bytes = [], 0
        batch.append(path)
        batch_bytes += size
    if batch:
        yield batch


def _disclose_delta_batch(*, index: int, total: int, n_files: int, batch_bytes: int) -> None:
    """One ``[transport]`` line per delta ship-batch (run-13 finding 3).

    Names the checkpoint cadence so a mid-push death is legible: each batch that
    lands is durable and its manifest checkpoint reflects remote reality, so a
    retry re-ships only the remainder. Fail-open like the sibling disclosures.
    """
    with contextlib.suppress(Exception):
        mb = batch_bytes / (1024 * 1024)
        print(
            f"[transport] content-hash DELTA: shipping batch {index}/{total} "
            f"({n_files} file(s), {mb:.1f} MB); the push manifest is checkpointed "
            "after it lands so a died-mid-push retry re-ships only the remainder",
            file=sys.stderr,
        )


def _load_hash_cache(root: Path) -> dict[str, dict[str, Any]]:
    """Read the local hash quick-check cache, or ``{}`` on any problem.

    A first push (no cache), an unreadable file, corrupt JSON, a wrong shape, or
    a schema-version mismatch all collapse to an empty cache — the scan then
    full-re-hashes and rewrites the cache. Fail-open: the cache is a pure
    optimization and never a correctness input.
    """
    path = root / _PUSH_HASH_CACHE_REL
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != _HASH_CACHE_VERSION:
        return {}
    entries = data.get("entries")
    if not isinstance(entries, dict):
        return {}
    return entries


def _store_hash_cache(root: Path, entries: dict[str, dict[str, Any]]) -> None:
    """Persist the quick-check cache atomically (fail-open, regenerable).

    Uses the repo's canonical :func:`atomic_write_json` (mkstemp sibling +
    ``os.replace``) so a torn write can never leave a corrupt cache. ``fsync``
    is skipped: the cache is a regenerable derived artifact — a crash that loses
    it only forces the next scan to re-hash (never a wrong sha), exactly the
    tradeoff :func:`atomic_write_json` documents for a derived cache.
    """
    from hpc_agent.infra.io import atomic_write_json

    with contextlib.suppress(OSError):
        atomic_write_json(
            root / _PUSH_HASH_CACHE_REL,
            {"version": _HASH_CACHE_VERSION, "entries": entries},
            fsync=False,
        )


def _build_local_manifest_cached(root: Path, paths: list[str]) -> tuple[Any, int, int]:
    """Build the local push manifest, reusing cached shas by (size, mtime_ns).

    Returns ``(manifest, n_hashed, n_changed_from_cache)``. For each path: if the
    cache holds an entry whose (size, mtime_ns) still match the file on disk, the
    cached sha256 is reused; otherwise the file is streamed-hashed and the cache
    updated. The rebuilt cache holds ONLY the current path set, so entries for
    files that vanished drop out. Missing paths raise ``FileNotFoundError`` —
    same hard-error contract as :func:`build_manifest` (a delta over a set you
    cannot read is a defect, not a silent omission).
    """
    # Reuse the manifest module's own primitives so a cached sha is
    # byte-for-byte identical to a fresh :func:`build_manifest` sha (same
    # streaming sha256, same FileEntry shape) — local and remote manifests must
    # stay in lockstep.
    from hpc_agent.infra.manifest import FileEntry, Manifest, _sha256_of

    cache = _load_hash_cache(root)
    entries: list[Any] = []
    new_cache: dict[str, dict[str, Any]] = {}
    n_hashed = 0
    n_cached = 0
    for rel in paths:
        rel_posix = Path(rel).as_posix()
        full = root / rel
        if not full.is_file():
            raise FileNotFoundError(f"manifest path not found under {root}: {rel}")
        st = full.stat()
        size = st.st_size
        mtime_ns = st.st_mtime_ns
        prior = cache.get(rel_posix)
        if (
            isinstance(prior, dict)
            and prior.get("size") == size
            and prior.get("mtime_ns") == mtime_ns
            and isinstance(prior.get("sha256"), str)
        ):
            sha = str(prior["sha256"])
            n_cached += 1
        else:
            sha = _sha256_of(full)
            n_hashed += 1
        entries.append(FileEntry(path=rel_posix, size=size, sha256=sha))
        new_cache[rel_posix] = {"size": size, "mtime_ns": mtime_ns, "sha256": sha}
    entries.sort(key=lambda e: e.path)
    _store_hash_cache(root, new_cache)
    return Manifest(entries=tuple(entries)), n_hashed, n_cached


def _local_push_manifest(local_path: str | Path, exclude: list[str]) -> Any:
    """Content manifest of the local push tree (exclude-filtered) — item 6b.

    Returns a :class:`hpc_agent.infra.manifest.Manifest`; imported lazily
    to keep this low-level infra module import-light. Backed by the local
    quick-check cache (run-13 finding 6) so a re-push re-hashes only the files
    that actually changed.
    """
    root = Path(local_path)
    paths = _pushable_relpaths(root, exclude)
    # Phase disclosure (run-#12 finding 3): hashing a multi-GB tree is
    # MINUTES of silence otherwise — the 8.7GB scan read as a hang twice in
    # one night. One line in, one line out, same stderr surface as the
    # transfer heartbeat.
    print(
        f"[transport] content-hash scan: checking {len(paths)} local file(s) "
        "for the push delta (cached shas reused by size+mtime; transfer follows)",
        file=sys.stderr,
    )
    manifest, n_hashed, n_cached = _build_local_manifest_cached(root, paths)
    print(
        f"[transport] content-hash scan done ({len(paths)} file(s); hashed "
        f"{n_hashed} changed, {n_cached} from cache); comparing against the "
        "remote manifest",
        file=sys.stderr,
    )
    return manifest


def _parse_remote_push_manifest(stdout: str) -> Any | None:
    """Parse the cluster-side hash manifest, or ``None`` on any problem.

    An absent/empty tree (snippet printed nothing), corrupt JSON, a wrong shape,
    or a cap breach all collapse to ``None`` — which routes the push to the
    full-copy tar fallback (disclosed). The safe direction: never claim a remote
    file is present unless the manifest proves it.
    """
    from hpc_agent.infra.manifest import Manifest

    raw = (stdout or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not (isinstance(data, dict) and isinstance(data.get("files"), list)):
        return None
    try:
        return Manifest.from_dict(data)
    except (KeyError, TypeError, ValueError):
        return None


def _remote_push_manifest(
    *, ssh_target: str, remote_path: str, exclude: list[str], timeout: float | None
) -> Any | None:
    """One bounded ssh round-trip: the deployed runtime hashes the remote tree.

    Ships :data:`_REMOTE_MANIFEST_SNIPPET` base64-piped into ``python3`` under
    ``remote_path`` and parses the JSON manifest it prints. Returns a
    :class:`Manifest` of the remote tree, or ``None`` when the remote can't
    produce one — a first deploy (``cd`` fails, absent tree), a pre-delta
    runtime, a python/base64 gap, a cap breach, or a timeout. ``None`` routes to
    the full-copy fallback (disclosed), so this is never worse than the prior
    whole-tree behavior. *remote_path* is ``shlex.quote``-d; the snippet is
    base64 (no shell metacharacters) so no source quoting is needed.
    """
    # ``_ssh_bounded`` is defined in the engine package (``__init__``), which
    # imports THIS module in its re-export block — import it call-time to keep
    # the package's own initialization free of an import cycle.
    from hpc_agent.infra.transport import _ssh_bounded

    b64 = base64.b64encode(_REMOTE_MANIFEST_SNIPPET.encode("utf-8")).decode("ascii")
    excludes_json = json.dumps([p.rstrip("/") for p in exclude])
    remote_cmd = (
        f"cd {shlex.quote(remote_path)} && printf %s {shlex.quote(b64)} | base64 -d | "
        f"HPC_DELTA_EXCLUDES={shlex.quote(excludes_json)} "
        f"HPC_DELTA_CAP={_DELTA_MANIFEST_FILE_CAP} python3"
    )
    try:
        proc = _ssh_bounded(
            ssh_target,
            remote_cmd,
            timeout=timeout,
            what=f"remote hash manifest of {remote_path}",
        )
    except (TimeoutError, OSError):
        return None
    return _parse_remote_push_manifest(getattr(proc, "stdout", "") or "")
