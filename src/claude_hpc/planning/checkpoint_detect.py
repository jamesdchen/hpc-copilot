"""Checkpoint-file detection for safe auto-daisy-chain decisions.

A daisy-chained job whose stage 1 doesn't checkpoint dies on preemption
and stage 2 starts from scratch — silently wasting compute. Auto-
daisy-chain only fires when we've seen this profile produce checkpoint
files in past runs.

False negatives (missed signal) are safe — the user just gets the
"exceeds max walltime" error and can opt in manually via
``auto_daisy_chain: true`` in clusters.yaml. False positives would
silently waste compute, so the detector is conservative: any error,
missing directory, or absent past-run signal yields ``False``.
"""

from __future__ import annotations

import json
from pathlib import Path

__all__ = ["detect_checkpointing"]

# Patterns matched against files inside the result_dir of past runs.
# Glob-style; case-insensitive matching applied per directory below so
# ``CHECKPOINT.PT`` and ``checkpoint.pt`` both count.
_CHECKPOINT_GLOBS: tuple[str, ...] = (
    "checkpoint*",
    "*.ckpt",
    "state*.pkl",
    "last*.pt",
    "latest*.pt",
    "model*.joblib",
    "model*.pkl",
    "model*.pt",
    "epoch_*.pt",
    "epoch_*.pkl",
)


def _matches_any_glob(name: str, globs: tuple[str, ...]) -> bool:
    """Case-insensitive fnmatch against any of *globs*."""
    from fnmatch import fnmatchcase

    lower = name.lower()
    return any(fnmatchcase(lower, g) for g in globs)


def _result_dir_candidates(experiment_dir: Path, *, profile: str, cluster: str) -> list[Path]:
    """Return absolute result_dir paths referenced by past sidecars matching
    ``(profile, cluster)``.

    Walks ``<exp>/.hpc/runs/*.json``, parses each, and pulls
    ``result_dir_template`` when present. Sidecars with no template
    (empty string after the v2 hardened-defaults backfill) are skipped.
    Templates that contain placeholders like ``{task_id}`` are mapped
    back to their parent directory (the run-scoped root) so we look at
    every per-task subdirectory's siblings; fall back to the literal
    string when no placeholder is present.
    """
    out: list[Path] = []
    runs_dir = experiment_dir / ".hpc" / "runs"
    if not runs_dir.is_dir():
        return out
    for sidecar in sorted(runs_dir.glob("*.json")):
        try:
            data = json.loads(sidecar.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("profile") != profile:
            continue
        if data.get("cluster") != cluster:
            continue
        tmpl = data.get("result_dir_template") or ""
        if not isinstance(tmpl, str) or not tmpl:
            continue
        # Strip placeholder segments — keep the longest literal-prefix
        # path. ``/scratch/.../run_42/task_{task_id}`` -> ``run_42``;
        # ``/.../{run_id}/{task_id}`` -> ``/...``. We then scan that
        # prefix recursively so any descendant checkpoint-shaped file
        # counts.
        literal_parts: list[str] = []
        for part in Path(tmpl).parts:
            if "{" in part or "}" in part:
                break
            literal_parts.append(part)
        if not literal_parts:
            continue
        # Refuse to walk filesystem root. A template whose first
        # non-root segment is a placeholder (e.g. "/run_{run_id}/...")
        # would otherwise rglob("/"), recursing every mount on the
        # node.
        if literal_parts == ["/"] or (len(literal_parts) == 1 and literal_parts[0] in ("/", "")):
            continue
        out.append(Path(*literal_parts))
    return out


def detect_checkpointing(
    experiment_dir: Path,
    *,
    profile: str,
    cluster: str,
) -> bool:
    """Return ``True`` when past runs of ``(profile, cluster)`` have written
    checkpoint-shaped files.

    Walks ``<experiment_dir>/.hpc/runs/*.json`` sidecars to find result-
    dir prefixes referenced by past runs of this ``(profile, cluster)``,
    then scans those prefixes recursively for any file whose name
    matches ``_CHECKPOINT_GLOBS`` (case-insensitive).

    Returns ``False`` on any error or when no signal is found — the
    detector is the gate for auto-daisy-chain, and silent waste of
    compute is far worse than a missed-signal false negative (the user
    can always set ``auto_daisy_chain: true`` explicitly).
    """
    try:
        candidates = _result_dir_candidates(experiment_dir, profile=profile, cluster=cluster)
    except Exception:  # noqa: BLE001 — defensive: ANY error -> False
        return False
    if not candidates:
        return False
    for prefix in candidates:
        try:
            if not prefix.exists() or not prefix.is_dir():
                continue
            for path in prefix.rglob("*"):
                try:
                    if path.is_file() and _matches_any_glob(path.name, _CHECKPOINT_GLOBS):
                        return True
                except OSError:
                    continue
        except OSError:
            continue
    return False
