"""Agent-diagnosis sidecar store — OPAQUE, provenance-marked, advisory-only.

The durable half of the park-time diagnosis seam (run-queue-placement
2026-07-28 §8 S13 shape: "parks carry pointers + the human reads renders from
disk"): when a run parks on an anomaly, a read-only investigator agent may
ATTACH a diagnosis dossier — a classification against the failure catalog,
log-evidence excerpts, drafted recovery options — and this module is where that
dossier durably lives, beside the run's other sidecar records::

    <experiment_dir>/.hpc/runs/<run_id>.diagnosis.json

Doctrine (the trust boundary, non-negotiable):

* the content is **agent judgment** and is stored as an opaque,
  provenance-marked proposal (``provenance.authored_by == "agent"``, stamped by
  THIS writer, never caller-supplied — a provenance the agent could set itself
  would be a guard the LLM satisfies, i.e. no guard);
* it is **display-only advisory matter**: it never enters the decision-brief
  provenance journal (:mod:`hpc_agent.state.decision_briefs`), never becomes an
  answer-menu option (the menu's "code-carried data only" rule means
  code-AUTHORED), and no gate reads it;
* surfaces carry **pointers + counts** (:func:`diagnosis_pointer` /
  :func:`diagnosis_pointer_line`), never the content — the human opens the file.

Storage discipline follows :mod:`hpc_agent.state.block_terminal` (the sibling
record one directory entry over — same idiom, no synced constants): atomic tmp
+ ``os.replace`` under an advisory flock, OVERWRITE on re-attach (newest
diagnosis wins — it is advisory, not an audit trail), and a fail-open reader
(absent / corrupt / unprovenanced → ``None``; a broken advisory file must
never break a status read).

Pure I/O: no ``_wire`` import, no SSH (the ``state/decision_briefs.py``
posture).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent.infra.io import advisory_flock
from hpc_agent.infra.time import utcnow_iso

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "AGENT_AUTHOR",
    "SCHEMA_VERSION",
    "diagnosis_path",
    "diagnosis_pointer",
    "diagnosis_pointer_line",
    "read_diagnosis",
    "write_diagnosis",
]

# Bump only on a breaking record-shape change; readers tolerate unknown extra
# keys (forward-compat), same policy as ``block_terminal.SCHEMA_VERSION``.
SCHEMA_VERSION = 1

#: The ONE provenance literal the writer stamps and the reader requires. A
#: record without it is not a valid diagnosis (fail-open ``None``) — so every
#: diagnosis any surface ever points at is, by construction, labelled
#: agent-authored.
AGENT_AUTHOR = "agent"

_log = logging.getLogger(__name__)


def _validate_run_id(run_id: str) -> None:
    """``run_id`` becomes a path segment — it must be fs-safe.

    Same refusal set as ``state.block_terminal._validate_segment`` (an idiom
    shared by convention, not a synced constant) so a diagnosis record can
    never escape the ``.hpc/runs/`` tree.
    """
    if not run_id:
        raise errors.SpecInvalid("run_id must be a non-empty string")
    if "/" in run_id or "\\" in run_id or run_id in (".", ".."):
        raise errors.SpecInvalid(f"run_id must be filesystem-safe; got {run_id!r}")


def diagnosis_path(experiment_dir: Path, run_id: str) -> Path:
    """The JSON path of *run_id*'s attached diagnosis (may not exist yet).

    Beside the run's ``.briefs.jsonl`` / ``.<block>.terminal.json`` sidecars.
    Raises :class:`errors.SpecInvalid` on a non-filesystem-safe *run_id*.
    """
    _validate_run_id(run_id)
    from hpc_agent._kernel.contract.layout import RepoLayout

    return RepoLayout(experiment_dir).runs / f"{run_id}.diagnosis.json"


def _lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".lock")


def write_diagnosis(
    experiment_dir: Path,
    *,
    run_id: str,
    classification: str,
    evidence_excerpts: list[dict[str, Any]],
    proposed_actions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Attach (OVERWRITE) *run_id*'s agent diagnosis; return the record written.

    Stamps the provenance envelope ``{authored_by: "agent", attached_at}``
    HERE — the caller cannot supply it, so a diagnosis that reads back is
    always honestly labelled. Re-attach overwrites (newest wins; the dossier is
    advisory, not append-only evidence). Atomic tmp + ``os.replace`` under a
    flock (the :func:`state.block_terminal.record_terminal` idiom) so a torn
    record is impossible.
    """
    path = diagnosis_path(experiment_dir, run_id)
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "provenance": {"authored_by": AGENT_AUTHOR, "attached_at": utcnow_iso()},
        "classification": classification,
        "evidence_excerpts": list(evidence_excerpts),
        "proposed_actions": list(proposed_actions),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with advisory_flock(_lock_path(path), timeout_sec=120.0):
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(record, fh, sort_keys=True, default=str)
            fh.flush()
            with contextlib.suppress(OSError):
                os.fsync(fh.fileno())
        os.replace(tmp, path)
    return record


def read_diagnosis(experiment_dir: Path, run_id: str) -> dict[str, Any] | None:
    """Return *run_id*'s attached diagnosis, or ``None`` — fail-open everywhere.

    ``None`` when the record is absent, unreadable, corrupt, or NOT
    provenance-marked as agent-authored (a record missing the stamp is treated
    as corrupt rather than laundered into an unlabelled proposal). A broken
    advisory file must never break the surface reading it.

    Raises :class:`errors.SpecInvalid` only on a bad *run_id* (a programmer
    error in the caller, distinct from a benign missing file).
    """
    path = diagnosis_path(experiment_dir, run_id)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        _log.warning("diagnosis: skipping unreadable %s (%s)", path, exc)
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        _log.warning("diagnosis: skipping corrupt %s (%s)", path, exc)
        return None
    if not isinstance(obj, dict):
        return None
    provenance = obj.get("provenance")
    if not (isinstance(provenance, dict) and provenance.get("authored_by") == AGENT_AUTHOR):
        _log.warning("diagnosis: skipping unprovenanced %s", path)
        return None
    return obj


def diagnosis_pointer(experiment_dir: Path, run_id: str) -> dict[str, Any] | None:
    """POINTER + COUNTS for a surface line, or ``None`` when nothing is attached.

    ``{path, attached_at, classification, proposed_actions_count}`` — exactly
    what the park notification / doctor / morning digest may carry (S13: the
    surface points, the human reads the render from disk). Never the excerpts,
    never the drafted actions. Fail-open like :func:`read_diagnosis`.
    """
    record = read_diagnosis(experiment_dir, run_id)
    if record is None:
        return None
    actions = record.get("proposed_actions")
    return {
        "path": str(diagnosis_path(experiment_dir, run_id)),
        "attached_at": str((record.get("provenance") or {}).get("attached_at") or ""),
        "classification": str(record.get("classification") or ""),
        "proposed_actions_count": len(actions) if isinstance(actions, list) else 0,
    }


def diagnosis_pointer_line(pointer: dict[str, Any] | None) -> str:
    """The ONE rendering of a diagnosis pointer every surface shares.

    ``"attached (N proposed action(s), agent-authored, advisory) — <path>"``
    when a diagnosis is attached, ``"none"`` otherwise — so the park
    notification, the doctor's parked note, and the morning digest cannot
    drift into three phrasings of the same pointer. The agent-authored label
    rides IN the line: a surface that shows the pointer shows the provenance.
    """
    if not pointer:
        return "none"
    count = pointer.get("proposed_actions_count") or 0
    path = pointer.get("path") or ""
    suffix = f" — {path}" if path else ""
    return f"attached ({count} proposed action(s), agent-authored, advisory){suffix}"
