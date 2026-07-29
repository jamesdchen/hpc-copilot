#!/usr/bin/env python3
"""Collect devx session-tag ledgers and their Claude Code sessions.

The repo-side half of the ``tag-session`` seam (the ONE devx affordance the
wheel ships — 2026-07-28 dev-loop/product separation). Product sessions
append opaque records to ``<experiment>/.hpc/devx/session_tags.jsonl``;
Claude Code keeps every session transcript under
``~/.claude/projects/<munged-cwd>/<session-id>.jsonl``. This script joins
the two by sweeping the projects directory — it recovers each project's real
working directory from the ``cwd`` field inside its transcripts (the munged
directory name is lossy: ``/`` becomes ``-``, indistinguishable from a real
hyphen), then reads that directory's tag ledger.

A COLLECTOR, not an interpreter: output is one JSON report on stdout —
per project: the recovered cwd, the session inventory (id, path, mtime),
and the raw tag records. Dedup, grouping, and meaning belong to whatever
consumes the report; tags are opaque here exactly as they are in core.

Usage::

    python scripts/devx_ingest.py [--projects-dir ~/.claude/projects] [--all]

``--all`` includes projects with sessions but no tag ledger (default: only
projects that have tags — the ones the dev loop was asked to look at).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Every parseable object line; torn/foreign lines skipped (ingestion data)."""
    out: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _project_cwd(project_dir: Path) -> str | None:
    """The project's real working directory, from any transcript's ``cwd`` field."""
    for transcript in sorted(project_dir.glob("*.jsonl")):
        for rec in _read_jsonl(transcript):
            cwd = rec.get("cwd")
            if isinstance(cwd, str) and cwd:
                return cwd
    return None


def _sessions(project_dir: Path) -> list[dict[str, Any]]:
    out = []
    for transcript in sorted(project_dir.glob("*.jsonl")):
        out.append(
            {
                "session_id": transcript.stem,
                "path": str(transcript),
                "mtime": transcript.stat().st_mtime,
            }
        )
    return out


def collect(projects_dir: Path, *, include_untagged: bool) -> list[dict[str, Any]]:
    report: list[dict[str, Any]] = []
    if not projects_dir.is_dir():
        return report
    for project_dir in sorted(p for p in projects_dir.iterdir() if p.is_dir()):
        cwd = _project_cwd(project_dir)
        ledger = Path(cwd) / ".hpc" / "devx" / "session_tags.jsonl" if cwd else None
        tags = _read_jsonl(ledger) if ledger and ledger.is_file() else []
        if not tags and not include_untagged:
            continue
        report.append(
            {
                "project_dir": str(project_dir),
                "cwd": cwd,
                "tags_ledger": str(ledger) if ledger else None,
                "tags": tags,
                "sessions": _sessions(project_dir),
            }
        )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--projects-dir",
        type=Path,
        default=Path.home() / ".claude" / "projects",
        help="Claude Code projects directory (default: ~/.claude/projects)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="include projects that have sessions but no tag ledger",
    )
    args = parser.parse_args(argv)
    report = collect(args.projects_dir, include_untagged=args.all)
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
