"""The run story — per-stream record→event projections + the one merge.

Design: ``docs/design/run-story.md`` (Wave A / T1, governed by decisions D1
sources, D2 merge, D3 event model). The run story is a PURE projection of a
run's complete journal trail into one deterministic, ordered, attributed
timeline — the decision journal's *interface* sibling. It journals nothing,
attests nothing, and interprets nothing: every event is IDENTITY (which
run/scope/section), ORDERING (merge by recorded ts), and COUNTING (sha
pointers, row/job counts) over opaque records — never what any record MEANS.

This module owns two things and only two:

* :class:`StoryEvent` — the frozen, 7-field event model (D3). Its shape is
  closed; no domain/metric/role vocabulary ever grows a field.
* the per-stream projections (one small function per D1 stream, closed ``kind``
  sets) and :func:`merge_events` — the ONE ordering definition (D2). A second
  re-sort anywhere else forks the timeline (the boundary-drift flag).

Reads route through the existing store readers
(:func:`~hpc_agent.state.decision_journal.read_decisions`,
:func:`~hpc_agent.state.decision_briefs.read_briefs`,
:func:`~hpc_agent.state.block_terminal.read_terminal`,
:func:`~hpc_agent.state.journal.load_run`, a tolerant read alongside
:func:`~hpc_agent.state.scopes.looks_path`, and — for the Class-C2 overnight
findings — the canonical ledger reader
:func:`~hpc_agent.ops.overnight.read_consumption_ledger` via a LAZY import, so
the ledger path/format keeps one definition without an eager ops dependency);
every one is tolerant of an absent/corrupt store (one bad record never strands
the trail — the tolerant-read doctrine).

Pure I/O-thin state module (the :mod:`hpc_agent.state.scopes` posture): no
``_wire`` import, no SSH, no mapreduce. The ``ops`` layer (Wave B / T4) owns
the Pydantic boundary, the header assembly, and the windowing/render.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from hpc_agent.state.block_terminal import read_terminal
from hpc_agent.state.decision_briefs import read_briefs
from hpc_agent.state.decision_journal import read_decisions
from hpc_agent.state.journal import load_run
from hpc_agent.state.scopes import looks_path

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

__all__ = [
    "STREAMS",
    "STREAM_RANK",
    "StoryEvent",
    "project_run_decisions",
    "project_briefs",
    "project_block_terminals",
    "project_journal_record",
    "project_scope_decisions",
    "project_looks",
    "project_notebook_decisions",
    "project_c2_findings",
    "merge_events",
    "build_story",
]

_log = logging.getLogger(__name__)

# ── the source-store stream nouns (D1) — the dossier's typing rule ────────────
# Each value is a concrete on-disk STORE NOUN, matching
# ``ops/export_dossier.py::DOSSIER_SOURCES`` (minus the opaque ``sidecar`` /
# ``aggregated`` stores, which contribute no timeline events — D1). ``stream``
# on an event is always one of these; nothing else.
_DECISION_JOURNAL = "decision-journal"
_BRIEFS = "briefs"
_BLOCK_TERMINAL = "block-terminal"
_JOURNAL_RECORD = "journal-record"
_SCOPE_JOURNAL = "scope-journal"
_LOOK_LEDGER = "look-ledger"
_NOTEBOOK_JOURNAL = "notebook-journal"
# The overnight consumption ledger (``ops/overnight.py::overnight_ledger_path`` —
# ``<run_id>.overnight.jsonl``). NOT a dossier-sealed store (yet): a story source
# ADDED by the overnight-repair taxonomy so a Class-C2 finding — a result-shaping
# / anomaly OBSERVATION the autonomous healer must never act on — lands in the run
# story as science (``docs/design/overnight-repair.md`` §4.4/§7.4/§10). Only the C2
# subset projects (the A/B/C1 lines stay the healer's own operational audit trail).
_OVERNIGHT_LEDGER = "overnight-ledger"

#: Every stream noun an event may be typed by (closed set).
STREAMS: frozenset[str] = frozenset(
    {
        _DECISION_JOURNAL,
        _BRIEFS,
        _BLOCK_TERMINAL,
        _JOURNAL_RECORD,
        _SCOPE_JOURNAL,
        _LOOK_LEDGER,
        _NOTEBOOK_JOURNAL,
        _OVERNIGHT_LEDGER,
    }
)

# ── the merge tie-break order (D2) ────────────────────────────────────────────
# Records from DIFFERENT writers land in the same second routinely (a block
# appends its brief, its terminal, and the human's decision inside one second).
# Ties on ``ts`` break by this fixed, documented stream order — chosen to match
# causal reality at a block boundary: brief → terminal → decision → scope →
# look → notebook → journal-record. This is a REPRESENTATION choice, pinned by
# test, NOT a truth claim.
#
# Note (D2, forced by D3's frozen 7-field model): the journal-record STAMPS
# (submitted/kill/superseded) and the ``verdict_history`` entries share the one
# ``journal-record`` noun and therefore one rank. D2's separate stamps→verdict
# position is realized by EMISSION ORDER — :func:`project_journal_record` emits
# the stamps before the verdict entries, and the stable merge preserves that
# within-second order (a stable sort keeps insertion order for equal keys).
#
# The overnight-ledger C2 finding is a DERIVED observation the overnight loop
# reaches AFTER the run's own records exist, so it ranks last (7) — a same-second
# tie sorts it after the journal-record stamps. Appended (not inserted) so no
# existing rank shifts and the pinned tie-break order is unchanged.
STREAM_RANK: dict[str, int] = {
    _BRIEFS: 0,
    _BLOCK_TERMINAL: 1,
    _DECISION_JOURNAL: 2,
    _SCOPE_JOURNAL: 3,
    _LOOK_LEDGER: 4,
    _NOTEBOOK_JOURNAL: 5,
    _JOURNAL_RECORD: 6,
    _OVERNIGHT_LEDGER: 7,
}

_HUMAN = "human"
_CODE = "code"

# The notebook block classes and their attestor (mirrors
# ``state/notebook_audit.py``): a sign-off is a HUMAN act; auto-clear and render
# receipt are CODE acts. Any other block riding the journal is projected as a
# generic code decision.
_NOTEBOOK_SIGN_OFF = "notebook-sign-off"
_NOTEBOOK_ATTESTOR = {
    _NOTEBOOK_SIGN_OFF: _HUMAN,
    "notebook-auto-clear": _CODE,
    "notebook-render-receipt": _CODE,
}

# The scope lock/unlock action key + values (mirrors ``state/scopes.py``).
_SCOPE_ACTION_KEY = "scope_action"
_SCOPE_LOCK = "lock"
_SCOPE_UNLOCK = "unlock"

# The Class-C2 overnight-finding record class + its fixed disposition (mirrors
# ``ops/recover/heal_taxonomy.py`` — the ``detail.heal_class == "C2"`` ledger
# lines). ``kind`` names the class + report-only nature; the disposition rides
# ``evidence`` so it renders explicitly beside the cause.
_C2_HEAL_CLASS = "C2"
_C2_FINDING = "c2-finding"
_C2_DISPOSITION = "report-only"


@dataclass(frozen=True)
class StoryEvent:
    """One typed, attributed timeline entry (D3). Shape is CLOSED.

    * ``ts`` — the recorded timestamp verbatim (``""`` when absent/malformed;
      such an event carries ``evidence["ts_missing"] = True`` and sorts to the
      epoch-front, never a crash).
    * ``stream`` — the SOURCE-STORE noun (one of :data:`STREAMS`).
    * ``actor`` — ``"human"`` | ``"code"`` (the attestation kernel's attestor
      vocabulary): human exactly for a decision ``response``, a scope unlock, a
      notebook sign-off, or a ``decided_by`` that is not ``"code"``.
    * ``kind`` — the record-class literal (block name, ``"scope-lock"``,
      ``"look"``, ``"verdict"``, ``"kill-requested"``, ... — closed per-stream
      sets).
    * ``subject_id`` — run_id / scope tag / audit section — OPAQUE identity.
    * ``evidence`` — sha pointers + counts ONLY (``cmd_sha``, ``*_digest``,
      ``*_sha``, ``lineage_root``, ``*_count``, ...); identity + counting,
      never a metric value.
    * ``text`` — the HUMAN's verbatim words when the record carries any (a
      nudge response, an unlock reason); else ``""``. Agent/code-drafted prose
      is NEVER text — only its sha digest rides ``evidence``.
    """

    ts: str
    stream: str
    actor: str
    kind: str
    subject_id: str
    evidence: dict[str, Any] = field(default_factory=dict)
    text: str = ""


# ── canonical digest (the one pointer helper) ─────────────────────────────────


def _digest(obj: Any) -> str:
    """sha256 of *obj*'s canonical JSON — a POINTER to agent-drafted content.

    The story never carries agent/code-drafted prose (a brief, a proposal, a
    verdict rationale); it carries only this fingerprint, so the render path can
    never re-launder LLM text as timeline narrative (D3). Sorted keys +
    ``default=str`` make it deterministic across insertion orders and platforms.
    """
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _ts_of(record: dict[str, Any]) -> tuple[str, bool]:
    """Return ``(ts, missing)`` for a record — tolerant, never fatal (D2).

    ``missing`` is True when the recorded ``ts`` is absent, non-string, or does
    not look like an ISO-8601 stamp (``YYYY-MM-DDT...`` — the shape every store
    writes via :func:`~hpc_agent.infra.time.utcnow_iso`). A missing ts becomes
    ``""`` so the event sorts to the epoch-front; the flag is surfaced, not a
    crash. No datetime PARSING happens — lexicographic compare IS chronological
    within this system (D2), so this is a shape check only.
    """
    raw = record.get("ts")
    if (
        isinstance(raw, str)
        and len(raw) >= 11
        and raw[4] == "-"
        and raw[7] == "-"
        and raw[10] == "T"
    ):  # noqa: E501
        return raw, False
    return "", True


def _with_ts(record: dict[str, Any], evidence: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Resolve a record's ts and fold a ``ts_missing`` flag into *evidence*."""
    ts, missing = _ts_of(record)
    if missing:
        evidence = {**evidence, "ts_missing": True}
    return ts, evidence


# ── per-stream projections (one per D1 stream; closed ``kind`` sets) ───────────


def project_run_decisions(records: Sequence[dict[str, Any]], run_id: str) -> list[StoryEvent]:
    """Project a run's decision journal (``scope_kind="run"``) — every record.

    Every ``append-decision`` record in a run's journal is a HUMAN act (a
    greenlight, a nudge, a reproduction receipt): ``actor="human"``, and the
    human's verbatim ``response`` renders as ``text``. The agent-drafted
    ``proposal`` / ``evidence_digest`` fields carry only their sha digest in
    ``evidence`` (never the prose — D3). ``kind`` is the record's ``block``
    literal.
    """
    out: list[StoryEvent] = []
    for record in records:
        evidence: dict[str, Any] = {}
        proposal = record.get("proposal")
        if proposal:
            evidence["proposal_digest"] = _digest(proposal)
        ev_digest = record.get("evidence_digest")
        if ev_digest:
            evidence["evidence_digest"] = _digest(ev_digest)
        ts, evidence = _with_ts(record, evidence)
        response = record.get("response")
        out.append(
            StoryEvent(
                ts=ts,
                stream=_DECISION_JOURNAL,
                actor=_HUMAN,
                kind=str(record.get("block") or ""),
                subject_id=run_id,
                evidence=evidence,
                text=response if isinstance(response, str) else "",
            )
        )
    return out


def project_briefs(records: Sequence[dict[str, Any]], run_id: str) -> list[StoryEvent]:
    """Project a run's emitted-brief journal — code emitted a brief at a boundary.

    A brief is code/agent-drafted evidence: ``actor="code"``, no ``text``, and
    only the brief's sha digest (``brief_digest``) rides ``evidence`` — never
    the brief's prose (D3). ``kind`` is the block that emitted it.
    """
    out: list[StoryEvent] = []
    for record in records:
        evidence: dict[str, Any] = {"brief_digest": _digest(record.get("brief") or {})}
        ts, evidence = _with_ts(record, evidence)
        out.append(
            StoryEvent(
                ts=ts,
                stream=_BRIEFS,
                actor=_CODE,
                kind=str(record.get("block") or ""),
                subject_id=run_id,
                evidence=evidence,
            )
        )
    return out


def project_block_terminals(experiment_dir: Path, run_id: str) -> list[StoryEvent]:
    """Project a run's detached-block terminals (glob the runs tree, D1).

    Enumerates every ``<run_id>.<block>.terminal.json`` under the sidecar runs
    tree the way ``export_dossier._gather_run`` does, then reads each through
    :func:`~hpc_agent.state.block_terminal.read_terminal`. ``actor="code"``;
    ``evidence`` carries the tree ``cmd_sha`` and the ``stage_reached`` control
    state (identity + state, never a metric). Blocks are visited in sorted name
    order for a stable within-second sequence.
    """
    from hpc_agent._kernel.contract.layout import RepoLayout

    out: list[StoryEvent] = []
    runs_dir = RepoLayout(experiment_dir).runs
    prefix = f"{run_id}."
    suffix = ".terminal.json"
    try:
        term_paths = sorted(runs_dir.glob(f"{run_id}.*.terminal.json"))
    except OSError as exc:  # pragma: no cover - defensive
        _log.warning("run_story: cannot glob terminals for %s (%s)", run_id, exc)
        return out
    for term_path in term_paths:
        block = term_path.name.removeprefix(prefix).removesuffix(suffix)
        record = read_terminal(experiment_dir, run_id, block)
        if record is None:
            continue
        result = record.get("result")
        result = result if isinstance(result, dict) else {}
        evidence: dict[str, Any] = {"cmd_sha": str(record.get("cmd_sha") or "")}
        stage = result.get("stage_reached")
        if stage:
            evidence["stage_reached"] = str(stage)
        ts, evidence = _with_ts(record, evidence)
        out.append(
            StoryEvent(
                ts=ts,
                stream=_BLOCK_TERMINAL,
                actor=_CODE,
                kind=str(record.get("block") or block),
                subject_id=run_id,
                evidence=evidence,
            )
        )
    return out


def project_journal_record(record: Any) -> list[StoryEvent]:
    """Synthesize the journal record's lifecycle stamps + verdict history (D1).

    ``record`` is a :class:`~hpc_agent.state.run_record.RunRecord` (or ``None``
    — no record yields no events). Emits, IN THIS ORDER (D2's stamps→verdict
    position, realized by emission order under the shared journal-record rank):

    * the timestamped lifecycle stamps — ``submitted`` (``submitted_at``),
      ``kill-requested`` / ``kill-confirmed`` (with a job COUNT, never ids),
      ``superseded`` (carrying the ``superseded_by`` identity) — each only when
      its stamp is present;
    * one ``verdict`` event per ``verdict_history`` entry, ``actor`` taken from
      the entry's own ``decided_by`` (``code``→code, anything else→human), the
      entry's rationale carried ONLY as a ``verdict_digest`` pointer.

    All are ``actor="code"`` except a non-``code`` verdict. No metric ever
    enters ``evidence``.
    """
    out: list[StoryEvent] = []
    if record is None:
        return out
    run_id = str(getattr(record, "run_id", "") or "")

    def _stamp(ts_val: Any, kind: str, evidence: dict[str, Any]) -> None:
        if isinstance(ts_val, str) and ts_val:
            ts, evidence2 = _with_ts({"ts": ts_val}, evidence)
            out.append(
                StoryEvent(
                    ts=ts,
                    stream=_JOURNAL_RECORD,
                    actor=_CODE,
                    kind=kind,
                    subject_id=run_id,
                    evidence=evidence2,
                )
            )

    _stamp(getattr(record, "submitted_at", None), "submitted", {})
    _stamp(
        getattr(record, "kill_requested_at", None),
        "kill-requested",
        {"job_count": len(getattr(record, "kill_requested_job_ids", []) or [])},
    )
    _stamp(
        getattr(record, "kill_confirmed_at", None),
        "kill-confirmed",
        {"job_count": len(getattr(record, "kill_confirmed_job_ids", []) or [])},
    )
    superseded_by = getattr(record, "superseded_by", "") or ""
    _stamp(
        getattr(record, "superseded_at", None),
        "superseded",
        {"superseded_by": superseded_by} if superseded_by else {},
    )

    for entry in getattr(record, "verdict_history", []) or []:
        if not isinstance(entry, dict):
            continue
        decided_by = entry.get("decided_by")
        actor = _CODE if decided_by == _CODE else _HUMAN
        evidence: dict[str, Any] = {"verdict_digest": _digest(entry)}
        if isinstance(decided_by, str) and decided_by:
            evidence["decided_by"] = decided_by
        ts, evidence = _with_ts({"ts": entry.get("applied_at")}, evidence)
        out.append(
            StoryEvent(
                ts=ts,
                stream=_JOURNAL_RECORD,
                actor=actor,
                kind="verdict",
                subject_id=run_id,
                evidence=evidence,
            )
        )
    return out


def project_scope_decisions(records: Sequence[dict[str, Any]], tag: str) -> list[StoryEvent]:
    """Project a scope's decision journal — lock/unlock (``resolved.scope_action``).

    Locking is the safe, code-reachable direction
    (:func:`~hpc_agent.state.scopes.record_lock`) → ``actor="code"``, kind
    ``"scope-lock"``, the reason carried only as a ``reason_digest`` pointer.
    An UNLOCK is a human act → ``actor="human"``, kind ``"scope-unlock"``, the
    human's verbatim reason as ``text``. ``subject_id`` is the opaque tag.
    """
    out: list[StoryEvent] = []
    for record in records:
        resolved = record.get("resolved")
        action = resolved.get(_SCOPE_ACTION_KEY) if isinstance(resolved, dict) else None
        response = record.get("response")
        if action == _SCOPE_UNLOCK:
            kind, actor = "scope-unlock", _HUMAN
        elif action == _SCOPE_LOCK:
            kind, actor = "scope-lock", _CODE
        else:
            kind, actor = "scope-decision", _CODE
        evidence: dict[str, Any] = {}
        if actor == _CODE and isinstance(response, str) and response:
            evidence["reason_digest"] = _digest(response)
        ts, evidence = _with_ts(record, evidence)
        out.append(
            StoryEvent(
                ts=ts,
                stream=_SCOPE_JOURNAL,
                actor=actor,
                kind=kind,
                subject_id=tag,
                evidence=evidence,
                text=response if actor == _HUMAN and isinstance(response, str) else "",
            )
        )
    return out


def project_looks(records: Sequence[dict[str, Any]], tag: str) -> list[StoryEvent]:
    """Project a scope's look ledger — one ``look`` per (scope, run) reduction.

    A look is recorded by code and carries IDENTITY ONLY (the ledger has no
    metric by its own rule): ``actor="code"``, ``kind="look"``,
    ``subject_id`` = the looking run's id, and ``evidence`` = the scope tag,
    ``cmd_sha``, ``lineage_root``, and ``reducer_block`` (all identity).
    """
    out: list[StoryEvent] = []
    for record in records:
        evidence: dict[str, Any] = {"scope": tag}
        for key in ("cmd_sha", "lineage_root", "reducer_block"):
            val = record.get(key)
            if val:
                evidence[key] = str(val)
        ts, evidence = _with_ts(record, evidence)
        out.append(
            StoryEvent(
                ts=ts,
                stream=_LOOK_LEDGER,
                actor=_CODE,
                kind="look",
                subject_id=str(record.get("run_id") or ""),
                evidence=evidence,
            )
        )
    return out


def project_notebook_decisions(
    records: Sequence[dict[str, Any]], audit_id: str
) -> list[StoryEvent]:
    """Project a notebook journal — sign-offs, auto-clears, render receipts (D1).

    Only records whose ``block`` is a notebook attestation class are projected
    (others are skipped). A sign-off is a HUMAN act (``actor="human"``, the
    ``response`` as ``text``); auto-clear and render receipt are CODE acts
    (``actor="code"``, no ``text``). ``subject_id`` is the section slug (opaque
    identity); ``evidence`` carries the sha pointers the record recorded
    (``section_sha`` / ``view_sha`` / ``output_sha``). ``kind`` is the block.
    """
    out: list[StoryEvent] = []
    for record in records:
        block = record.get("block")
        attestor = _NOTEBOOK_ATTESTOR.get(block) if isinstance(block, str) else None
        if attestor is None:
            continue
        resolved = record.get("resolved")
        resolved = resolved if isinstance(resolved, dict) else {}
        evidence: dict[str, Any] = {}
        for key in ("section_sha", "view_sha", "output_sha"):
            val = resolved.get(key)
            if val:
                evidence[key] = str(val)
        ts, evidence = _with_ts(record, evidence)
        response = record.get("response")
        out.append(
            StoryEvent(
                ts=ts,
                stream=_NOTEBOOK_JOURNAL,
                actor=attestor,
                kind=str(block),
                subject_id=str(resolved.get("section") or ""),
                evidence=evidence,
                text=response if attestor == _HUMAN and isinstance(response, str) else "",
            )
        )
    return out


def project_c2_findings(records: Sequence[dict[str, Any]], run_id: str) -> list[StoryEvent]:
    """Project a run's overnight consumption ledger — the Class-C2 findings ONLY (D1).

    The overnight-repair taxonomy (``docs/design/overnight-repair.md`` §4.4 / §7.4)
    routes a Class-C2 finding — a result-shaping / anomaly OBSERVATION the autonomous
    healer must never act on (a version upgrade, a winsorize, a retry-selection, a raw
    result anomaly) — OUT of the infra brief and INTO the run story as science. A
    result anomaly is a finding, and the story is where science lands.

    Only ledger lines whose ``detail.heal_class`` is ``"C2"`` project. The A / B heals
    and C1 parks are OPERATIONAL — the healer's own audit trail, kept by the morning
    brief's ``class_morning_sections`` — not observations ABOUT the experiment, so they
    are skipped here (the ``project_notebook_decisions`` non-attestation-block precedent).

    ``actor="code"`` (a finding is code-composed, never a human act); ``kind`` is the
    ``c2-finding`` record class; ``evidence`` carries the CAUSE slug + the class + the
    report-only disposition — identity/classification literals, never a metric value
    (``subject_id`` is the run whose ledger this is). ``ts`` is the finding's
    ``failed_at`` — when it happened overnight (the ``project_journal_record`` stamp
    idiom: ``_with_ts`` over the recorded timestamp key).
    """
    out: list[StoryEvent] = []
    for record in records:
        detail = record.get("detail")
        detail = detail if isinstance(detail, dict) else {}
        if str(detail.get("heal_class") or "") != _C2_HEAL_CLASS:
            continue
        evidence: dict[str, Any] = {
            "cause": str(detail.get("cause") or ""),
            "heal_class": _C2_HEAL_CLASS,
            "disposition": _C2_DISPOSITION,
        }
        ts, evidence = _with_ts({"ts": record.get("failed_at")}, evidence)
        out.append(
            StoryEvent(
                ts=ts,
                stream=_OVERNIGHT_LEDGER,
                actor=_CODE,
                kind=_C2_FINDING,
                subject_id=run_id,
                evidence=evidence,
            )
        )
    return out


# ── the ONE merge (D2) ────────────────────────────────────────────────────────


def merge_events(events: Iterable[StoryEvent]) -> list[StoryEvent]:
    """Merge events into the ONE deterministic timeline (D2). The only ordering.

    The merge key is the triple ``(ts, stream_rank, intra_stream_index)``:

    * ``ts`` — lexicographic compare of the ISO-8601 stamp IS chronological
      within this system (no datetime parsing). A missing ts is ``""`` and
      sorts to the epoch-front.
    * ``stream_rank`` — same-second cross-writer ties break by
      :data:`STREAM_RANK`.
    * ``intra_stream_index`` — append order within one stream is causal by
      construction and is NEVER reordered. This is realized by Python's STABLE
      sort: for equal ``(ts, stream_rank)`` the input order is preserved, so a
      caller that feeds each stream's events in append order (as
      :func:`build_story` does) gets intra-file order for free.

    A second re-sort of the result anywhere else forks the timeline (the
    boundary-drift flag) — this is the single ordering definition.
    """
    return sorted(events, key=lambda e: (e.ts, STREAM_RANK.get(e.stream, len(STREAM_RANK))))


# ── the build-story entry point (Wave B / T4 consumes this) ───────────────────


def build_story(
    experiment_dir: Path,
    *,
    run_ids: Sequence[str],
    scope_tags: Sequence[str] = (),
    notebook_audit_ids: Sequence[str] = (),
) -> list[StoryEvent]:
    """Read every D1 source for the given ids and merge into one timeline.

    *run_ids* are the run(s) whose per-run stores are read (a single run, or a
    supersession lineage the caller resolved via
    :func:`~hpc_agent.state.scopes.lineage_chain`); *scope_tags* and
    *notebook_audit_ids* are the scope / notebook journals the caller gathered
    off the sidecar. Every read is tolerant of an absent/corrupt store (empty is
    data, never an error) — an empty run (no records anywhere) yields ``[]``, an
    empty story, not a failure.

    This does the reads (routed through the existing store readers) and the D2
    merge; the ``ops`` layer (T4) decides WHICH ids (lineage, sidecar scope
    tags, the ``audited_source`` echo) and owns the header, windowing, and
    render. Events are fed to :func:`merge_events` in append order per stream so
    the stable merge preserves intra-file order.
    """
    events: list[StoryEvent] = []
    for run_id in run_ids:
        events.extend(project_run_decisions(read_decisions(experiment_dir, "run", run_id), run_id))
        events.extend(project_briefs(read_briefs(experiment_dir, run_id), run_id))
        events.extend(project_block_terminals(experiment_dir, run_id))
        events.extend(project_journal_record(load_run(experiment_dir, run_id)))
        c2_records = _read_overnight_c2_ledger(experiment_dir, run_id)
        events.extend(project_c2_findings(c2_records, run_id))
    for tag in scope_tags:
        events.extend(project_scope_decisions(read_decisions(experiment_dir, "scope", tag), tag))
        events.extend(project_looks(_read_looks(experiment_dir, tag), tag))
    for audit_id in notebook_audit_ids:
        events.extend(
            project_notebook_decisions(
                read_decisions(experiment_dir, "notebook", audit_id), audit_id
            )
        )
    return merge_events(events)


def _read_overnight_c2_ledger(experiment_dir: Path, run_id: str) -> list[dict[str, Any]]:
    """Tolerant read of a run's overnight consumption ledger (the tolerant-read idiom).

    Routes through the canonical ops-layer reader
    (:func:`~hpc_agent.ops.overnight.read_consumption_ledger`) via a LAZY import — the
    ledger's path + JSONL format live there (one-definition), and the lazy import keeps
    the state module free of an eager ops dependency (the ``RepoLayout`` lazy-import
    idiom in :func:`project_block_terminals`). Any failure yields ``[]`` — an absent or
    torn overnight ledger never strands the rest of a run's trail.
    """
    try:
        from hpc_agent.ops.overnight import read_consumption_ledger

        return read_consumption_ledger(experiment_dir, "run", run_id)
    except Exception as exc:  # noqa: BLE001 - tolerant read: one bad ledger never strands the trail
        _log.warning("run_story: skipping unreadable overnight ledger for %s (%s)", run_id, exc)
        return []


def _read_looks(experiment_dir: Path, tag: str) -> list[dict[str, Any]]:
    """Tolerant read of a scope's look ledger (the tolerant-read idiom, D1).

    A read alongside :func:`~hpc_agent.state.scopes.looks_path` — ``[]`` on an
    absent ledger, blank / individually-corrupt lines skipped with a warning so
    one bad line never strands the rest of the trail.
    """
    path = looks_path(experiment_dir, tag)
    records: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return records
    except (OSError, UnicodeDecodeError) as exc:
        _log.warning("run_story: skipping unreadable look ledger %s (%s)", path, exc)
        return records
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            _log.warning("run_story: skipping corrupt line %d in %s (%s)", lineno, path, exc)
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records
