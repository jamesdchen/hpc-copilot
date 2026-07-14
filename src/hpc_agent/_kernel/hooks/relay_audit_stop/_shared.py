"""Shared primitives for the relay-audit Stop hook subpackage.

Journal-namespace resolvers (no-scaffold), transcript parsing, the mention
scans, and the two finding NamedTuples — the substrate every audit pass and the
output composers build on. Kept dependency-free of the sibling audit modules so
the import graph stays acyclic (``__init__`` → audits → ``_shared``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, NamedTuple


class _AbsentMarker(NamedTuple):
    """An undischarged relay-due marker whose key tokens the relay never carried.

    Rejector: :attr:`omission_text` is the verbatim-ready block reason (today's
    string). Completer: :attr:`marker` is the resolved dict from which the owed
    artifact is composed (D4) and the completer-discharge is recorded (D3).
    """

    scope_kind: str
    scope_id: str
    marker: dict[str, Any]
    omission_text: str


class _Violation(NamedTuple):
    """A relayed claim that contradicts the durable record (violation class §2).

    Rejector: :attr:`text` is today's finding line. Completer: appended as a
    code-authored correction UNDER the claim, EXCEPT when the poisoned-decision
    test fires (a run/campaign scope with a still-pending brief whose content the
    claim tokens intersect), where it bounces instead. ``claim``/``journal_value``
    drive the correction and the poisoned intersection; an empty ``claim`` (a
    paraphrase / audit-scope finding) is append-only by construction.
    """

    scope_kind: str
    scope_id: str
    claim: str
    journal_value: str | None
    text: str


def _journal_runs_dir(experiment_dir: Path) -> Path:
    """``<journal home>/<repo_hash>/runs`` — WITHOUT creating (no-scaffold)."""
    from hpc_agent.state.run_record import current_homedir, repo_hash

    return current_homedir() / repo_hash(experiment_dir) / "runs"


def _notebook_audits_dir(experiment_dir: Path) -> Path:
    """``<experiment>/.hpc/notebooks`` — WITHOUT creating (no-scaffold).

    Constructed as a raw path (never ``RepoLayout(...).hpc``, which materializes
    the ``.hpc`` tree) so the discovery probe stays side-effect-free — a repo that
    has never run an audit is not scaffolded one by a Stop event.
    """
    return Path(experiment_dir).resolve() / ".hpc" / "notebooks"


def final_assistant_text(transcript_path: Path) -> str:
    """The final assistant message text from a session transcript, or ``""``.

    The transcript is JSONL, one message per line; the final relay is the
    trailing run of ``type == "assistant"`` entries (a single logical reply
    may span several assistant lines). Text blocks are joined in order.
    Tolerant: unreadable file or corrupt lines yield ``""`` / skip the line.
    """
    try:
        text = transcript_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return ""

    entries: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            entries.append(obj)

    trailing: list[dict[str, Any]] = []
    for entry in reversed(entries):
        if entry.get("type") == "assistant":
            trailing.append(entry)
        elif trailing:
            break
        elif entry.get("type") in ("user", "human", "system"):
            # A non-assistant message before any assistant tail → no final
            # assistant text (the turn ended without a reply?). Keep scanning
            # only while we have not started a tail.
            break
    trailing.reverse()

    parts: list[str] = []
    for entry in trailing:
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            parts.append(content)
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                block_text = block.get("text")
                if isinstance(block_text, str) and block_text:
                    parts.append(block_text)
    return "\n".join(parts)


def mentioned_run_ids(relay_text: str, runs_dir: Path) -> list[str]:
    """Journaled run ids the relay text actually names, journal order.

    A claim is only attributable to a run the relay mentions, so the audit is
    keyed on substring presence of each ``<runs>/<run_id>.json`` stem in the
    final text. Filesystem errors yield an empty list (fail-open).
    """
    try:
        stems = sorted(p.stem for p in runs_dir.glob("*.json"))
    except OSError:
        return []
    return [rid for rid in stems if rid and rid in relay_text]


def mentioned_audit_ids(relay_text: str, notebooks_dir: Path) -> list[str]:
    """Notebook audit ids the relay text names, journal order.

    Mirrors :func:`mentioned_run_ids`: keyed on substring presence of each
    ``<notebooks>/<audit_id>.decisions.jsonl`` stem in the final text — a claim
    is only attributable to an audit the relay mentions. A glob-only probe (no
    journal is read) so a stop that names no audit does zero notebook work.
    Filesystem errors yield an empty list (fail-open).
    """
    try:
        ids = sorted(
            p.name[: -len(".decisions.jsonl")] for p in notebooks_dir.glob("*.decisions.jsonl")
        )
    except OSError:
        return []
    return [aid for aid in ids if aid and aid in relay_text]
