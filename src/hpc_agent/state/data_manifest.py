"""The data manifest — rung 0 of the onboarding map (``docs/design/data-manifest.md``).

Converts data changes from **invisible to attributed**. The manifest records an
identity (``sha256`` + ``size`` + opaque ``built_by``) for every file under the
experiment's DECLARED input roots, so the quiet-corruption class — same filename,
silently rebuilt bytes, every downstream result subtly wrong and nothing ever
throwing — becomes a mechanical drift observation instead of manual archaeology.

Agnosticism is held six ways (the boundary test): core hashes opaque bytes and
**never** parses a format — there is no ``pyarrow`` / ``pandas`` import in this
module, no ``data/`` convention (roots are the caller's ONE existing declaration),
and ``built_by`` is caller free text carried opaquely, never validated.

Two hash disciplines, each in its own lane (allowlisted in the grep lint):

* **file-content shas** are RAW-byte hashes of the file (:func:`file_sha256`);
* the **manifest-doc sha** is a canonical-JSON hash of the records
  (:func:`manifest_doc_sha`) — the identity of the manifest AS A DOCUMENT, the
  thing the journaled mint records.

Performance: a ``(size, mtime)`` content-keyed cache (the ``describe_cache``
precedent) so a re-mint never re-hashes an unchanged gigabyte, and a drift read
never re-hashes an unchanged file.

Pure local I/O + hashing — no SSH, no ``_wire`` import, no scheduler.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hpc_agent.infra.io import append_jsonl_line, atomic_write_json
from hpc_agent.infra.time import utcnow_iso

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "SCHEMA_VERSION",
    "MANIFEST_RELPATH",
    "DriftReport",
    "manifest_path",
    "cache_path",
    "journal_path",
    "read_manifest",
    "declared_input_roots",
    "manifest_doc_sha",
    "file_sha256",
    "build_records",
    "mint_manifest",
    "compute_drift",
]

SCHEMA_VERSION = 1

#: Home (ruled 0a): sits with ``interview.json`` / ``axes.yaml`` as a
#: copilot-consumed caller record, git-trackable, machine-minted.
MANIFEST_RELPATH = ".hpc/data_manifest.json"
#: The ``(size, mtime)`` fast-path cache — a derived accelerator, never the SoT
#: (the manifest is). Regenerable from the files on disk, so it is fsync-free.
_CACHE_RELPATH = ".hpc/data_manifest.cache.json"
#: The mint journal — the tier-0 "who changed the data, when" timeline the repo
#: otherwise lacks. Append-only via the canonical JSONL seam.
_JOURNAL_RELPATH = ".hpc/data_manifest.journal.jsonl"

#: Bytes read per hash chunk (raw-byte discipline, streamed so a gigabyte file
#: never loads whole into memory).
_HASH_CHUNK = 1 << 20


# ── paths ─────────────────────────────────────────────────────────────────────


def manifest_path(experiment_dir: Path | str, *, output_path: str | None = None) -> Path:
    """The manifest file path — ``<experiment_dir>/.hpc/data_manifest.json`` by default.

    ``output_path`` (the spec override) is resolved relative to *experiment_dir*
    when relative, honored as-is when absolute. Does not create anything.
    """
    base = Path(experiment_dir)
    if output_path:
        p = Path(output_path)
        return p if p.is_absolute() else base / p
    return base / MANIFEST_RELPATH


def cache_path(experiment_dir: Path | str) -> Path:
    """The ``(size, mtime)`` cache path (does not create it)."""
    return Path(experiment_dir) / _CACHE_RELPATH


def journal_path(experiment_dir: Path | str) -> Path:
    """The mint journal path (does not create it)."""
    return Path(experiment_dir) / _JOURNAL_RELPATH


# ── the ONE input-roots declaration read (mirrors ops/notebook_gate._read_audited_source) ──


def declared_input_roots(experiment_dir: Path | str) -> list[str] | None:
    """The experiment's DECLARED input roots, or ``None`` when none is declared.

    ONE "what are my inputs" declaration in the system (the one-definition rule
    applied to a declaration): interview.json's ``audited_source.input_roots``.
    Mirrors :func:`hpc_agent.ops.notebook_gate._read_audited_source`'s posture —
    the canonical campaign-dir root, ``.hpc/interview.json`` accepted defensively;
    a missing file, a corrupt/non-object file, or an absent block all read as "not
    declared" → ``None``. A hardcoded ``data/`` default is REFUSED by design (core
    never guesses which directories are data), so ``None`` here means the verb
    refuses without ``roots`` in the spec.

    Returns a NON-EMPTY list, or ``None`` — an empty ``input_roots`` reads as
    "declared nothing", which is not a usable default either.
    """
    base = Path(experiment_dir)
    for rel in ("interview.json", ".hpc/interview.json"):
        path = base / rel
        if not path.is_file():
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        block = doc.get("audited_source")
        if not isinstance(block, dict):
            continue
        roots = block.get("input_roots")
        if isinstance(roots, list):
            clean = [r for r in roots if isinstance(r, str) and r]
            if clean:
                return clean
    return None


# ── hashing (two disciplines, each in its lane) ───────────────────────────────


def file_sha256(path: Path) -> str:
    """RAW-byte sha256 hexdigest of a file, streamed in chunks.

    The file-content discipline — the exact bytes on disk, no normalization (that
    is the notebook-audit source discipline's lane, deliberately separate).
    """
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(_HASH_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _canonical_json(value: Any) -> str:
    """Canonicalize *value* to a stable, whitespace-free, key-sorted JSON string.

    P-S1: the canonical-JSON discipline appears several places
    (``ops/check_task_generator_mismatch.py::canonical_json``,
    ``ops/notebook/audit_view._canonical_json``). This is a LOCAL definition to
    keep the manifest module free of a cross-subject import; it is a UNIFICATION
    CANDIDATE for the P-S1 canonical-JSON helper when that lands.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def manifest_doc_sha(records: dict[str, Any]) -> str:
    """The manifest-doc sha: sha256 over the canonical JSON of the *records* map.

    The identity of the manifest AS A DOCUMENT (the ``{relpath: {...}}`` map),
    NOT a raw-byte file hash — computed over :func:`_canonical_json` so key order
    and whitespace never move it. This is what the journaled mint records as the
    "this is the new known-good data identity" fingerprint.
    """
    return hashlib.sha256(_canonical_json(records).encode("utf-8")).hexdigest()


# ── the (size, mtime) fast-path cache ─────────────────────────────────────────


def _read_cache(experiment_dir: Path) -> dict[str, dict[str, Any]]:
    """The ``(size, mtime) -> sha256`` cache, or ``{}`` on any miss/error (read-only)."""
    path = cache_path(experiment_dir)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = doc.get("entries") if isinstance(doc, dict) else None
    return entries if isinstance(entries, dict) else {}


def _cached_sha(
    cache: dict[str, dict[str, Any]], relpath: str, size: int, mtime_ns: int
) -> str | None:
    """Reuse the cached sha for *relpath* IFF ``(size, mtime)`` still match; else ``None``."""
    entry = cache.get(relpath)
    if not isinstance(entry, dict):
        return None
    if entry.get("size") == size and entry.get("mtime_ns") == mtime_ns:
        sha = entry.get("sha256")
        return sha if isinstance(sha, str) else None
    return None


# ── file discovery under the declared roots ───────────────────────────────────


def _relpath(experiment_dir: Path, path: Path) -> str:
    """POSIX relpath of *path* under *experiment_dir* (falls back to str when it escapes)."""
    try:
        return path.resolve().relative_to(experiment_dir.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _iter_files(experiment_dir: Path, roots: Sequence[str]) -> list[tuple[str, Path]]:
    """Every regular file under the declared *roots*, as ``(relpath, path)`` pairs.

    A root may be a file or a directory; directories are walked recursively. The
    ``.hpc`` control tree is skipped (the manifest never records itself). Order is
    deterministic (sorted by relpath). Non-existent roots contribute nothing —
    a missing declared root surfaces later as ``missing`` records, never a crash.
    """
    seen: dict[str, Path] = {}
    for root in roots:
        base = experiment_dir / root
        if base.is_file():
            seen[_relpath(experiment_dir, base)] = base
            continue
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            rel = _relpath(experiment_dir, p)
            if rel.startswith(".hpc/") or rel == ".hpc":
                continue
            seen[rel] = p
    return sorted(seen.items())


# ── mint ──────────────────────────────────────────────────────────────────────


def build_records(
    experiment_dir: Path | str,
    roots: Sequence[str],
    *,
    prior: dict[str, Any] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Build the ``{relpath: {sha256, size, built_by?}}`` records + the refreshed cache.

    Uses the ``(size, mtime)`` fast-path cache so an unchanged file is never
    re-hashed. ``built_by`` is carried OPAQUELY from *prior* (the previous
    manifest's records) for any file that persists — core stores and echoes it,
    never validates it. Returns ``(records, cache_entries)``; the caller persists
    both.
    """
    base = Path(experiment_dir)
    cache = _read_cache(base)
    prior_files = (prior or {}).get("files") if isinstance(prior, dict) else None
    prior_files = prior_files if isinstance(prior_files, dict) else {}

    records: dict[str, dict[str, Any]] = {}
    new_cache: dict[str, dict[str, Any]] = {}
    for rel, path in _iter_files(base, roots):
        stat = path.stat()
        size = stat.st_size
        mtime_ns = stat.st_mtime_ns
        sha = _cached_sha(cache, rel, size, mtime_ns) or file_sha256(path)
        entry: dict[str, Any] = {"sha256": sha, "size": size}
        # Opaque carry: preserve caller-authored built_by across a re-mint.
        prior_entry = prior_files.get(rel)
        if isinstance(prior_entry, dict) and prior_entry.get("built_by") is not None:
            entry["built_by"] = prior_entry["built_by"]
        records[rel] = entry
        new_cache[rel] = {"size": size, "mtime_ns": mtime_ns, "sha256": sha}
    return records, new_cache


def mint_manifest(
    experiment_dir: Path | str,
    roots: Sequence[str],
    *,
    output_path: str | None = None,
) -> dict[str, Any]:
    """Mint (or re-mint) the manifest over *roots*; journal the act; refresh the cache.

    Writes ``<experiment_dir>/.hpc/data_manifest.json`` (or *output_path*)
    atomically, appends a ``mint`` record to the mint journal carrying the
    manifest-doc sha (the tier-0 "who changed the data, when" timeline), and
    refreshes the ``(size, mtime)`` cache. Re-minting IS the journaled "this is
    the new known-good" act. Returns the written manifest document.
    """
    base = Path(experiment_dir)
    prior = read_manifest(base, output_path=output_path)
    records, new_cache = build_records(base, roots, prior=prior)
    doc_sha = manifest_doc_sha(records)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "roots": list(roots),
        "files": records,
        "manifest_doc_sha": doc_sha,
        "minted_at": utcnow_iso(),
    }
    out = manifest_path(base, output_path=output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(out, manifest)

    cache_out = cache_path(base)
    cache_out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(cache_out, {"entries": new_cache}, fsync=False)

    append_jsonl_line(
        journal_path(base),
        {
            "schema_version": SCHEMA_VERSION,
            "ts": manifest["minted_at"],
            "action": "mint",
            "manifest_doc_sha": doc_sha,
            "roots": list(roots),
            "file_count": len(records),
        },
    )
    return manifest


def read_manifest(
    experiment_dir: Path | str, *, output_path: str | None = None
) -> dict[str, Any] | None:
    """The manifest document, or ``None`` when absent/unreadable (read-only, tolerant)."""
    path = manifest_path(Path(experiment_dir), output_path=output_path)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


# ── drift (read-only, verdict-free) ───────────────────────────────────────────


@dataclass(frozen=True)
class DriftReport:
    """The verdict-FREE drift projection: counts + identities, humans conclude.

    ``unmanifested`` marks "no manifest at read time" (the standing-disclosure
    case). Otherwise ``matched`` / ``drifted`` / ``new`` / ``missing`` are sorted
    relpath lists — ``drifted`` = a TRACKED file whose bytes changed (the
    quiet-corruption class), ``missing`` = a TRACKED file gone, ``new`` = an
    untracked file appeared under a declared root. Core NEVER labels a change
    "updated / corrupted / restated" — that meaning is human judgment.
    """

    unmanifested: bool
    matched: tuple[str, ...] = ()
    drifted: tuple[str, ...] = ()
    new: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()

    @property
    def counts(self) -> dict[str, int]:
        return {
            "matched": len(self.matched),
            "drifted": len(self.drifted),
            "new": len(self.new),
            "missing": len(self.missing),
        }

    @property
    def has_drift(self) -> bool:
        """True when a TRACKED file changed or vanished (the needs-attention class)."""
        return bool(self.drifted or self.missing)


def _current_sha(path: Path, cache: dict[str, dict[str, Any]], rel: str) -> str:
    """Current file sha via the ``(size, mtime)`` fast-path (read-only — never writes cache)."""
    stat = path.stat()
    return _cached_sha(cache, rel, stat.st_size, stat.st_mtime_ns) or file_sha256(path)


def compute_drift(experiment_dir: Path | str, *, output_path: str | None = None) -> DriftReport:
    """Compare the on-disk files under the manifest's roots to the recorded identities.

    Read-only: reads the manifest + the ``(size, mtime)`` cache and stats/hashes
    the current files, but writes NOTHING (never re-mints, never refreshes the
    cache — a read must not mutate). Unchanged files (matching ``(size, mtime)``)
    are not re-hashed. Returns a :class:`DriftReport`; a missing manifest yields
    ``unmanifested=True`` (the standing-disclosure case).
    """
    base = Path(experiment_dir)
    manifest = read_manifest(base, output_path=output_path)
    if manifest is None:
        return DriftReport(unmanifested=True)

    recorded = manifest.get("files")
    recorded = recorded if isinstance(recorded, dict) else {}
    roots = manifest.get("roots")
    roots = [r for r in roots if isinstance(r, str)] if isinstance(roots, list) else []
    cache = _read_cache(base)

    current = dict(_iter_files(base, roots))
    matched: list[str] = []
    drifted: list[str] = []
    missing: list[str] = []
    for rel, entry in recorded.items():
        recorded_sha = entry.get("sha256") if isinstance(entry, dict) else None
        path = current.pop(rel, None)
        if path is None or not path.is_file():
            missing.append(rel)
            continue
        if _current_sha(path, cache, rel) == recorded_sha:
            matched.append(rel)
        else:
            drifted.append(rel)
    new = list(current)  # under a declared root, not in the recorded set
    return DriftReport(
        unmanifested=False,
        matched=tuple(sorted(matched)),
        drifted=tuple(sorted(drifted)),
        new=tuple(sorted(new)),
        missing=tuple(sorted(missing)),
    )
