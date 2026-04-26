"""Manifest filename conventions, staleness handling, and resume helpers.

Today, rerunning ``/submit`` against an identical grid would silently overwrite
a single ``_hpc_dispatch.json`` on disk — losing the prior manifest and making
it impossible to compare runs after fixing executor code.

This module introduces content-addressed manifest filenames:

* :func:`manifest_filename_for_sha` turns a per-run ``cmd_sha`` into the
  canonical filename ``manifest.<cmd_sha_short>.json``.
* :func:`aggregate_cmd_sha` computes a deterministic run-level hash from a
  manifest's per-task ``cmd_sha`` values — used when callers want a single
  "this manifest" fingerprint.
* :func:`write_manifest` writes the content-addressed file *and* keeps a
  plain ``manifest.json`` pointing at the latest manifest (symlink where the
  filesystem supports it, fallback copy otherwise) for back-compat with any
  caller that opens ``manifest.json`` directly.
* :func:`find_existing_manifests` / :func:`find_manifest_by_cmd_sha` let the
  slash-command layer detect prior runs with matching ``cmd_sha``.
* :func:`prune_old_manifests` evicts the oldest manifests past a retention
  cap (default :data:`MAX_MANIFESTS`).
* :func:`build_manifest_with_resume` threads an optional ``resume_from`` path
  through to :func:`~hpc_mapreduce.job.resubmit.resubmit_plan` so the Python
  layer can expose a single "build or resume" entry point while the
  interactive prompt remains in ``agent/commands/submit.md``.

Back-compat: the manifest *contents* are unchanged; only the on-disk
filename convention is additive.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hpc_mapreduce.job.constraints import ClusterConstraints
    from hpc_mapreduce.job.resubmit import ResubmitPlan

__all__ = [
    "MAX_MANIFESTS",
    "MANIFEST_ALIAS",
    "manifest_filename_for_sha",
    "aggregate_cmd_sha",
    "write_manifest",
    "find_existing_manifests",
    "find_manifest_by_cmd_sha",
    "prune_old_manifests",
    "build_manifest_with_resume",
]

# Maximum number of per-experiment manifests retained on disk. Oldest-first
# eviction by mtime. Module-level so callers (and tests) can monkeypatch.
MAX_MANIFESTS: int = 10

# Canonical alias pointing at the most recent manifest. Kept for back-compat
# with any code/tooling that opens ``manifest.json`` directly (on-cluster
# dispatcher, ``/monitor`` fallback paths, etc.).
MANIFEST_ALIAS: str = "manifest.json"

# Matches ``manifest.<cmd_sha_short>.json`` where ``cmd_sha_short`` is 1-64
# hex chars. The short form we emit is exactly 8 chars; the wider accept
# range keeps the matcher forward-compatible if we ever widen the prefix.
_MANIFEST_FILENAME_RE = re.compile(r"^manifest\.([0-9a-f]{1,64})\.json$")


def manifest_filename_for_sha(cmd_sha: str) -> str:
    """Return the canonical manifest filename for a given ``cmd_sha``.

    The filename is ``manifest.<cmd_sha_short>.json`` where
    ``cmd_sha_short`` is the first 8 characters of *cmd_sha*.

    Raises
    ------
    ValueError
        If *cmd_sha* is empty or shorter than 8 characters.
    """
    if not cmd_sha:
        raise ValueError("cmd_sha must be a non-empty hex string")
    if len(cmd_sha) < 8:
        raise ValueError(f"cmd_sha must be at least 8 hex chars for naming (got {len(cmd_sha)})")
    short = cmd_sha[:8].lower()
    if not re.fullmatch(r"[0-9a-f]{8}", short):
        raise ValueError(f"cmd_sha prefix must be hex: {short!r}")
    return f"manifest.{short}.json"


def aggregate_cmd_sha(manifest: dict) -> str:
    """Compute a deterministic run-level SHA from a manifest's per-task shas.

    Concatenates the per-task ``cmd_sha`` strings in sorted-by-task-id order
    and returns the SHA-256 of the result. Each task's ``cmd_sha`` is the
    first 16 hex chars of ``SHA-256(cmd)`` (see
    :mod:`hpc_mapreduce.job.grid`), so this function hashes a hash — the
    resulting digest is stable across equivalent grids and changes whenever
    any task's command changes.

    Returns a 64-char hex string. Callers that want the short form pass the
    result to :func:`manifest_filename_for_sha`.

    Raises
    ------
    ValueError
        If any task lacks a ``cmd_sha`` (i.e. the manifest was produced by
        a pre-v2 builder).
    """
    tasks = manifest.get("tasks", {})
    try:
        ordered = sorted(tasks.items(), key=lambda item: int(item[0]))
    except (ValueError, TypeError):
        ordered = sorted(tasks.items())
    parts: list[str] = []
    for tid, entry in ordered:
        sha = entry.get("cmd_sha") if isinstance(entry, dict) else None
        if not sha:
            raise ValueError(
                f"task {tid!r} is missing 'cmd_sha'; aggregate_cmd_sha requires v2 manifests"
            )
        parts.append(sha)
    joined = "\n".join(parts).encode()
    return hashlib.sha256(joined).hexdigest()


def _update_alias(experiment_dir: Path, target_name: str) -> None:
    """Point ``manifest.json`` at *target_name* inside *experiment_dir*.

    Prefers a relative symlink; falls back to a plain-file copy when
    symlinks are unavailable (Windows without developer mode, for example).
    """
    alias = experiment_dir / MANIFEST_ALIAS
    target = experiment_dir / target_name
    if alias.is_symlink() or alias.exists():
        with contextlib.suppress(OSError):
            alias.unlink()
    try:
        os.symlink(target_name, alias)
        return
    except (OSError, NotImplementedError):
        # Fall through to copy-fallback.
        pass
    # Copy-fallback: preserves the contract that reading ``manifest.json``
    # returns the latest manifest's contents. Best-effort — if we cannot
    # write the alias we still succeeded on the primary write.
    with contextlib.suppress(OSError):
        alias.write_bytes(target.read_bytes())


def write_manifest(
    experiment_dir: Path,
    manifest: dict,
    cmd_sha: str | None = None,
) -> Path:
    """Write *manifest* as ``manifest.<cmd_sha_short>.json`` in *experiment_dir*.

    Also updates the ``manifest.json`` alias to point at the new file, and
    prunes the oldest manifests past :data:`MAX_MANIFESTS`.

    Parameters
    ----------
    experiment_dir:
        Directory that owns this manifest. Created if missing.
    manifest:
        The manifest dict as produced by
        :func:`hpc_mapreduce.job.grid.build_task_manifest`.
    cmd_sha:
        Optional pre-computed run-level SHA. When omitted it is computed
        from the manifest via :func:`aggregate_cmd_sha`.

    Returns
    -------
    Path to the content-addressed manifest file that was written.
    """
    experiment_dir.mkdir(parents=True, exist_ok=True)
    if cmd_sha is None:
        cmd_sha = aggregate_cmd_sha(manifest)
    filename = manifest_filename_for_sha(cmd_sha)
    target = experiment_dir / filename
    target.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    _update_alias(experiment_dir, filename)
    prune_old_manifests(experiment_dir, keep=MAX_MANIFESTS)
    return target


def find_existing_manifests(experiment_dir: Path) -> list[Path]:
    """Return every ``manifest.<sha>.json`` file in *experiment_dir*.

    Results are sorted newest-first by mtime. The plain ``manifest.json``
    alias is excluded (it's a pointer, not a distinct manifest).
    """
    if not experiment_dir.exists():
        return []
    hits: list[Path] = []
    for entry in experiment_dir.iterdir():
        if not entry.is_file():
            continue
        if entry.name == MANIFEST_ALIAS:
            continue
        if _MANIFEST_FILENAME_RE.match(entry.name):
            hits.append(entry)
    hits.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return hits


def find_manifest_by_cmd_sha(experiment_dir: Path, cmd_sha: str) -> Path | None:
    """Return the manifest file matching *cmd_sha*, or ``None`` if absent.

    Matches on the first 8 hex chars — the same prefix used for the filename.
    """
    try:
        target_name = manifest_filename_for_sha(cmd_sha)
    except ValueError:
        return None
    candidate = experiment_dir / target_name
    if candidate.is_file():
        return candidate
    return None


def prune_old_manifests(experiment_dir: Path, keep: int = MAX_MANIFESTS) -> list[Path]:
    """Evict oldest manifests past the retention cap. Returns the deleted paths.

    Ordering is by mtime (newest retained). The plain ``manifest.json``
    alias is never deleted even if it happens to match the pattern.
    """
    if keep < 0:
        raise ValueError("keep must be non-negative")
    hits = find_existing_manifests(experiment_dir)
    if len(hits) <= keep:
        return []
    to_delete = hits[keep:]
    deleted: list[Path] = []
    for path in to_delete:
        try:
            path.unlink()
            deleted.append(path)
        except OSError:
            # Skip anything we cannot delete — retention is best-effort.
            continue
    return deleted


def build_manifest_with_resume(
    manifest: dict,
    resume_from: Path | None,
    failed_task_ids: list[int] | None = None,
    overrides: dict | None = None,
    constraints: ClusterConstraints | None = None,
) -> dict | ResubmitPlan:
    """Dispatch between fresh-submit and resume code paths.

    When *resume_from* is ``None`` the freshly-built *manifest* is returned
    unchanged — callers should write it via :func:`write_manifest`. When a
    path is provided, the prior manifest is loaded and the list of failed
    task IDs is handed to
    :func:`hpc_mapreduce.job.resubmit.resubmit_plan` to produce a
    :class:`~hpc_mapreduce.job.resubmit.ResubmitPlan`.

    The interactive resume-vs-fresh decision lives in
    ``agent/commands/submit.md`` — this helper is the Python side of that
    hand-off: the slash-command tells us which branch to take.

    Parameters
    ----------
    manifest:
        The freshly-built manifest (only consulted when
        ``resume_from is None``).
    resume_from:
        Path to a prior manifest on disk, or ``None`` for a fresh run.
    failed_task_ids:
        Task IDs to rerun when resuming. Required iff *resume_from* is set.
    overrides, constraints:
        Passed through to
        :func:`~hpc_mapreduce.job.resubmit.resubmit_plan` unchanged.

    Raises
    ------
    ValueError
        If ``resume_from`` is set but ``failed_task_ids`` is empty or
        missing.
    FileNotFoundError
        If ``resume_from`` does not exist.
    """
    if resume_from is None:
        return manifest

    if not failed_task_ids:
        raise ValueError(
            "build_manifest_with_resume: failed_task_ids is required when resume_from is set"
        )

    resume_path = Path(resume_from)
    if not resume_path.is_file():
        raise FileNotFoundError(f"resume_from manifest not found: {resume_path}")

    prior = json.loads(resume_path.read_text())

    # Local import avoids a circular dependency at module import time —
    # ``resubmit`` already imports ``grid``/``constraints`` which this
    # module lives alongside.
    from hpc_mapreduce.job.resubmit import resubmit_plan

    return resubmit_plan(
        prior,
        failed_task_ids=failed_task_ids,
        overrides=overrides,
        constraints=constraints,
    )
