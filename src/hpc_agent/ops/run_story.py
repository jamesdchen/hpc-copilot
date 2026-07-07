"""``run-story`` — render a run's complete journal trail as one ordered timeline.

A read-only ``query`` primitive (run-story Wave B / T4, decision D5). Given a
``run_id`` it gathers every D1 source store the run left behind — the run
decision journal, the emitted briefs, the detached-block terminals, the journal
record's lifecycle stamps + verdict history, and (keyed off the run's sidecar)
the scope journals + look ledgers for each scope tag and the notebook
attestation journal when the sidecar echoes an audited source — merges them into
the ONE deterministic, ordered, attributed timeline
(:func:`hpc_agent.state.run_story.build_story` → the single ``merge_events``
definition), windows it honestly (D6), and renders canonical JSON + ``story_sha``
+ code-authored markdown (:func:`hpc_agent.ops.story_render.render_story`).

It is a PURE projection (the ``ops/notebook_status.py`` posture): no SSH, no
scheduler, no write, no store. Derived state recomputed from the on-disk records
on every call, so it can never drift from a second source of truth. It never
interprets what any record MEANS — every event is IDENTITY (which
run/scope/section), ORDERING (recorded ts), and COUNTING (sha pointers, row/job
counts) over opaque records (the boundary posture pinned by
``tests/contracts/test_run_story_boundary.py``).

This file lives at the ``ops/`` *role root* (sibling to ``export_dossier.py`` /
``notebook_status.py``) because it reads across subjects — the ``state`` sidecar,
the decision/brief/terminal journals, the scope substrate + look ledgers, the
journal records, and the notebook attestation journal. The subject-imports lint
short-circuits for role-root files, so the cross-subject reads here are allowed
by construction.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.queries.run_story import (
    RunStoryEvent,
    RunStoryResult,
    RunStorySpec,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.ops.story_render import render_story
from hpc_agent.state import scopes as _scopes
from hpc_agent.state.journal import load_run
from hpc_agent.state.run_story import build_story
from hpc_agent.state.runs import read_run_sidecar, run_sidecar_path

if TYPE_CHECKING:
    from collections.abc import Sequence

    from hpc_agent.state.run_story import StoryEvent

__all__ = ["run_story"]


def _safe_sidecar(experiment_dir: Path, run_id: str) -> dict[str, Any]:
    """Return a run's parsed sidecar dict, or ``{}`` when none exists.

    A missing sidecar is DATA, not an error (the ``export_dossier._safe_sidecar``
    precedent) — a run with a journal record but no sidecar still has a story.
    The parse lives inside :func:`state.runs.read_run_sidecar`; this module never
    parses the bytes it reads.
    """
    try:
        return read_run_sidecar(experiment_dir, run_id)
    except FileNotFoundError:
        return {}


def _audit_id_of(sidecar: dict[str, Any]) -> str:
    """The opaque audit slug the sidecar echoed, or ``""`` (the ``audited_source`` echo).

    The notebook attestation journal is a story source ONLY when the sidecar
    carries an ``audited_source`` block (the D1 rule; the ``export_dossier``
    precedent). The slug is opaque identity — which audit sealed the run — never
    interpreted.
    """
    echo = sidecar.get("audited_source")
    if isinstance(echo, dict):
        audit_id = echo.get("audit_id")
        if isinstance(audit_id, str) and audit_id:
            return audit_id
    return ""


def _gather_ids(experiment_dir: Path, run_ids: Sequence[str]) -> tuple[list[str], list[str]]:
    """Union the scope tags + notebook audit ids across the run set (sidecar-keyed).

    Scope tags come off each run's sidecar exactly as ``export_dossier`` unions
    them, so the story's sources and the dossier's sealed stores can never
    disagree about what a run's trail IS. Insertion-ordered, de-duplicated, so a
    lineage that carries a tag twice reads it once. A run with no sidecar
    contributes nothing (empty is data).
    """
    scope_tags: dict[str, None] = {}
    audit_ids: dict[str, None] = {}
    for rid in run_ids:
        sidecar = _safe_sidecar(experiment_dir, rid)
        for tag in sidecar.get("scopes") or []:
            if isinstance(tag, str) and tag:
                scope_tags.setdefault(tag, None)
        audit_id = _audit_id_of(sidecar)
        if audit_id:
            audit_ids.setdefault(audit_id, None)
    return list(scope_tags), list(audit_ids)


def _window(
    events: list[StoryEvent], *, since_ts: str | None, limit: int | None
) -> tuple[list[StoryEvent], int, int]:
    """Apply the honest D6 window; return ``(windowed, total_events, omitted_count)``.

    ``since_ts`` is a lexicographic ISO-8601 FLOOR (valid because D2 pins the
    stamp format — no datetime parsing); ``limit`` keeps the most recent N events
    (a newest-LAST window over the already-ordered list). ``total_events`` is the
    FULL count before ANY window; ``omitted_count`` is what the window dropped.
    Both counts ride the render pre-image, so a window can never masquerade as
    the whole story and ``story_sha`` can never be passed off as covering events
    it does not contain (D6). This NEVER re-sorts — the list is already the one
    merge order, and re-ordering it would fork the timeline (the boundary flag).
    """
    total = len(events)
    windowed = events
    if since_ts is not None:
        windowed = [e for e in windowed if e.ts >= since_ts]
    if limit is not None and limit < len(windowed):
        windowed = windowed[len(windowed) - limit :]
    return windowed, total, total - len(windowed)


def _header(
    experiment_dir: Path,
    run_id: str,
    run_ids: Sequence[str],
    sidecar: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the story header from the RunRecord + sidecar (direct field reads).

    ``{run_ids, cluster, submitted_at, status, scopes, audit_id?, supersedes?}``
    read directly off :class:`~hpc_agent.state.run_record.RunRecord` fields + the
    sidecar (D4) — deliberately NOT a copy of the dossier's
    ``_project_run_identity`` allowlist (the one-definition note: if this ever
    converges on that projection, promote the dossier's private function to a
    shared symbol rather than fork it). ``audit_id`` / ``supersedes`` are emitted
    only when present (the reproduces-if-present idiom). Every value is IDENTITY —
    no metric, no role.
    """
    record = load_run(experiment_dir, run_id)
    header: dict[str, Any] = {
        "run_ids": list(run_ids),
        "cluster": (getattr(record, "cluster", "") or "") if record is not None else "",
        "submitted_at": (getattr(record, "submitted_at", "") or "") if record is not None else "",
        "status": (getattr(record, "status", "") or "") if record is not None else "",
        "scopes": [str(t) for t in (sidecar.get("scopes") or []) if t],
    }
    audit_id = _audit_id_of(sidecar)
    if audit_id:
        header["audit_id"] = audit_id
    supersedes = (getattr(record, "supersedes", "") or "") if record is not None else ""
    if supersedes:
        header["supersedes"] = supersedes
    return header


@primitive(
    name="run-story",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key=None,
    cli=CliShape(
        help=(
            "Render a run's complete journal trail as ONE deterministic, ordered, "
            "attributed timeline — decision journal, briefs, block terminals, "
            "journal-record stamps + verdict history, scope journals + look "
            "ledgers, and the notebook attestation journal — fingerprinted by "
            "story_sha. Read-only, no SSH. --include-lineage widens the read to the "
            "run's whole supersession chain. since_ts / limit window honestly: the "
            "result carries total_events / omitted_count and the markdown surfaces "
            "the omission, so a window never masquerades as the whole story. It is "
            "a PURE projection — identity, ordering, and counting over opaque "
            "records; it never interprets what any record means."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=RunStorySpec,
        schema_ref=SchemaRef(input="run_story"),
    ),
    agent_facing=True,
)
def run_story(*, experiment_dir: Path, spec: RunStorySpec) -> RunStoryResult:
    """Merge a run's D1 sources into one ordered, windowed, fingerprinted timeline.

    Resolves the run set (the single run, or its whole supersession lineage via
    the ONE ``lineage_chain`` walk when ``include_lineage``), gathers the scope
    tags + notebook audit id off each run's sidecar, builds the story through the
    single ``merge_events`` definition, applies the honest D6 window, assembles
    the header, and renders canonical JSON + ``story_sha`` + markdown.

    Idempotent by construction: derived state recomputed from the on-disk records
    on every call. No store, no write, no attestation — ``story_sha`` is a
    FINGERPRINT, not a claim about content.

    Raises :class:`errors.SpecInvalid` when the requested run has NEITHER a
    sidecar NOR a journal record (nothing to render — the ``export_dossier``
    no-sidecar-no-record guard). An absent individual store is DATA, never an
    error — an empty run yields an empty story, not a failure.
    """
    experiment_dir = Path(experiment_dir)
    run_id = spec.run_id

    # Missing-run refusal (the export_dossier precedent): no sidecar AND no
    # journal record → there is nothing to render.
    has_sidecar = run_sidecar_path(experiment_dir, run_id).is_file()
    if not has_sidecar and load_run(experiment_dir, run_id) is None:
        raise errors.SpecInvalid(
            f"no run sidecar or journal record found for run_id {run_id!r} — nothing to render"
        )

    # Resolve the run set: the single run, or its whole supersession lineage
    # (newest→root) via the ONE lineage definition (no second walk here).
    run_ids = _scopes.lineage_chain(experiment_dir, run_id) if spec.include_lineage else [run_id]

    scope_tags, notebook_audit_ids = _gather_ids(experiment_dir, run_ids)

    events = build_story(
        experiment_dir,
        run_ids=run_ids,
        scope_tags=scope_tags,
        notebook_audit_ids=notebook_audit_ids,
    )
    windowed, total_events, omitted_count = _window(
        events, since_ts=spec.since_ts, limit=spec.limit
    )

    header = _header(experiment_dir, run_id, run_ids, _safe_sidecar(experiment_dir, run_id))
    render = render_story(
        header,
        windowed,
        total_events=total_events,
        omitted_count=omitted_count,
        markdown=spec.markdown,
    )

    return RunStoryResult(
        run_ids=list(run_ids),
        events=[
            RunStoryEvent(
                ts=e.ts,
                stream=e.stream,
                actor=e.actor,
                kind=e.kind,
                subject_id=e.subject_id,
                evidence=dict(e.evidence),
                text=e.text,
            )
            for e in windowed
        ],
        story_sha=render.story_sha,
        markdown=render.markdown,
        total_events=total_events,
        omitted_count=omitted_count,
    )
