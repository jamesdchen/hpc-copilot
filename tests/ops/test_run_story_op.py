"""Tests for the ``run-story`` assembly op (``ops/run_story.py``, T4).

Exercises the primitive end-to-end over a tmp experiment with REAL records (the
same writers the state-layer test uses): the happy path with the decision /
brief / terminal / journal-record / scope / look / notebook streams; lineage
union; window honesty (``total_events`` / ``omitted_count``); the missing-run
refusal; the ``markdown=False`` opt-out; and the one-ordering guarantee (the op's
events are exactly ``build_story``'s merge order).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent import errors
from hpc_agent._wire.queries.run_story import RunStorySpec
from hpc_agent.ops.run_story import run_story
from hpc_agent.state.block_terminal import record_terminal
from hpc_agent.state.decision_briefs import append_brief
from hpc_agent.state.decision_journal import append_decision
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord
from hpc_agent.state.run_story import build_story
from hpc_agent.state.runs import write_run_sidecar
from hpc_agent.state.scopes import record_look

if TYPE_CHECKING:
    from pathlib import Path

_TS = "2026-07-08T12:00:00+00:00"


def _run_record(run_id: str, **overrides: object) -> RunRecord:
    base = dict(
        run_id=run_id,
        profile="p",
        cluster="hoffman2",
        ssh_target="host",
        remote_path="/remote",
        job_name="job",
        job_ids=["1"],
        total_tasks=1,
        submitted_at=_TS,
        experiment_dir="/exp",
    )
    base.update(overrides)
    return RunRecord(**base)  # type: ignore[arg-type]


def _sidecar(experiment_dir: Path, run_id: str, **kw: object) -> None:
    write_run_sidecar(
        experiment_dir,
        run_id=run_id,
        cmd_sha="cmdsha",
        hpc_agent_version="0.0.0",
        submitted_at=_TS,
        executor="exec.py",
        result_dir_template="results/{i}",
        task_count=1,
        tasks_py_sha="tsha",
        cluster="hoffman2",
        **kw,  # type: ignore[arg-type]
    )


def _seed_run(experiment_dir: Path, run_id: str, ts: str = _TS, **sidecar_kw: object) -> None:
    """Write a full stream set for *run_id* (sidecar + record + the per-run journals)."""
    _sidecar(experiment_dir, run_id, **sidecar_kw)
    upsert_run(experiment_dir, _run_record(run_id, submitted_at=ts))
    append_decision(
        experiment_dir,
        scope_kind="run",
        scope_id=run_id,
        block="submit-s2",
        response="ship it",
        proposal="secret agent prose",
        ts=ts,
    )
    append_brief(experiment_dir, run_id=run_id, block="submit-s2", brief={"m": 0.9}, ts=ts)
    record_terminal(
        experiment_dir,
        run_id=run_id,
        block="submit-s2",
        cmd_sha="cmdsha",
        result_dump={"stage_reached": "canary_verified", "block": "submit-s2"},
    )


# ── happy path: every stream merges + fingerprint ─────────────────────────────


def test_happy_path_all_streams(tmp_path: Path) -> None:
    _seed_run(tmp_path, "r1", scopes=["holdout"])
    # scope + look streams (keyed off the sidecar's scopes tag)
    append_decision(
        tmp_path,
        scope_kind="scope",
        scope_id="holdout",
        block="scope-lock",
        response="freeze it",
        resolved={"scope_action": "lock"},
        ts=_TS,
    )
    record_look(
        tmp_path, "holdout", run_id="r1", cmd_sha="csha", lineage_root="r1", reducer_block="agg"
    )

    result = run_story(experiment_dir=tmp_path, spec=RunStorySpec(run_id="r1"))

    assert result.run_ids == ["r1"]
    streams = {e.stream for e in result.events}
    assert {
        "decision-journal",
        "briefs",
        "block-terminal",
        "journal-record",
        "scope-journal",
        "look-ledger",
    } <= streams
    assert len(result.story_sha) == 64
    assert result.total_events == len(result.events)
    assert result.omitted_count == 0
    assert result.markdown.startswith("# Run story")
    # agent-drafted proposal prose never rides the wire — only its digest.
    assert "secret agent prose" not in result.markdown
    for ev in result.events:
        assert "secret agent prose" not in ev.text


def test_notebook_stream_when_sidecar_echoes_audited_source(tmp_path: Path) -> None:
    _sidecar(
        tmp_path,
        "r1",
        audited_source={"source": "src.py", "template": "tpl.py", "audit_id": "audit-1"},
    )
    upsert_run(tmp_path, _run_record("r1"))
    append_decision(
        tmp_path,
        scope_kind="notebook",
        scope_id="audit-1",
        block="notebook-sign-off",
        response="y",
        resolved={"section": "fit-model", "section_sha": "sha-fit", "view_sha": "view-1"},
        ts=_TS,
    )

    result = run_story(experiment_dir=tmp_path, spec=RunStorySpec(run_id="r1"))
    nb = [e for e in result.events if e.stream == "notebook-journal"]
    assert len(nb) == 1
    assert nb[0].actor == "human" and nb[0].kind == "notebook-sign-off"
    assert result.markdown  # header carries the audit_id
    assert "audit_id" in result.markdown


# ── lineage union ─────────────────────────────────────────────────────────────


def test_include_lineage_unions_the_chain(tmp_path: Path) -> None:
    # r2 supersedes r1 → the lineage chain is [r2, r1].
    _seed_run(tmp_path, "r1", ts="2026-07-08T11:00:00+00:00")
    _sidecar(tmp_path, "r2")
    upsert_run(tmp_path, _run_record("r2", supersedes="r1", submitted_at=_TS))
    append_decision(
        tmp_path, scope_kind="run", scope_id="r2", block="submit-s2", response="ok", ts=_TS
    )

    single = run_story(experiment_dir=tmp_path, spec=RunStorySpec(run_id="r2"))
    lineage = run_story(
        experiment_dir=tmp_path, spec=RunStorySpec(run_id="r2", include_lineage=True)
    )

    assert single.run_ids == ["r2"]
    assert lineage.run_ids == ["r2", "r1"]
    # the lineage story strictly widens the event set (r1's stores join).
    assert lineage.total_events > single.total_events
    subjects = {e.subject_id for e in lineage.events}
    assert "r1" in subjects and "r2" in subjects


# ── window honesty (D6) ───────────────────────────────────────────────────────


def test_limit_window_reports_honest_counts(tmp_path: Path) -> None:
    _seed_run(tmp_path, "r1")
    full = run_story(experiment_dir=tmp_path, spec=RunStorySpec(run_id="r1"))
    total = full.total_events
    assert total >= 3

    windowed = run_story(experiment_dir=tmp_path, spec=RunStorySpec(run_id="r1", limit=2))
    assert len(windowed.events) == 2
    assert windowed.total_events == total  # full count before the window
    assert windowed.omitted_count == total - 2
    assert f"showing 2 of {total} events" in windowed.markdown
    assert "omitted" in windowed.markdown
    # the window keeps the NEWEST events (a newest-last window over merge order).
    assert windowed.events == full.events[total - 2 :]
    # the windowed sha differs from the full sha (counts ride the pre-image).
    assert windowed.story_sha != full.story_sha


def test_since_ts_floor_drops_and_counts_older_events(tmp_path: Path) -> None:
    _sidecar(tmp_path, "r1")
    upsert_run(tmp_path, _run_record("r1", submitted_at="2026-07-08T10:00:00+00:00"))
    append_decision(
        tmp_path,
        scope_kind="run",
        scope_id="r1",
        block="s1",
        response="early",
        ts="2026-07-08T10:00:00+00:00",
    )
    append_decision(
        tmp_path,
        scope_kind="run",
        scope_id="r1",
        block="s2",
        response="late",
        ts="2026-07-08T14:00:00+00:00",
    )

    result = run_story(
        experiment_dir=tmp_path,
        spec=RunStorySpec(run_id="r1", since_ts="2026-07-08T12:00:00+00:00"),
    )
    kept_ts = [e.ts for e in result.events]
    assert all(ts >= "2026-07-08T12:00:00+00:00" for ts in kept_ts)
    assert result.omitted_count > 0
    assert result.total_events == len(result.events) + result.omitted_count


# ── refusal + tolerance + opt-out + ordering ──────────────────────────────────


def test_unknown_run_is_spec_invalid(tmp_path: Path) -> None:
    try:
        run_story(experiment_dir=tmp_path, spec=RunStorySpec(run_id="ghost"))
    except errors.SpecInvalid as exc:
        assert "ghost" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected SpecInvalid for an unknown run_id")


def test_absent_stores_are_data_not_error(tmp_path: Path) -> None:
    # A run with ONLY a journal record (no sidecar, no per-run journals) renders
    # an empty-but-valid story — the missing stores are data, not a failure.
    upsert_run(tmp_path, _run_record("r1"))
    result = run_story(experiment_dir=tmp_path, spec=RunStorySpec(run_id="r1"))
    assert result.run_ids == ["r1"]
    # only the journal-record 'submitted' stamp exists.
    assert {e.stream for e in result.events} == {"journal-record"}
    assert result.omitted_count == 0


def test_markdown_opt_out(tmp_path: Path) -> None:
    _seed_run(tmp_path, "r1")
    result = run_story(experiment_dir=tmp_path, spec=RunStorySpec(run_id="r1", markdown=False))
    assert result.markdown == ""
    assert result.story_sha  # fingerprint still computed


def test_events_are_exactly_the_merge_order(tmp_path: Path) -> None:
    # The op never re-sorts: its events are build_story's merge order verbatim.
    _seed_run(tmp_path, "r1", scopes=["holdout"])
    record_look(
        tmp_path, "holdout", run_id="r1", cmd_sha="c", lineage_root="r1", reducer_block="agg"
    )
    result = run_story(experiment_dir=tmp_path, spec=RunStorySpec(run_id="r1"))
    merged = build_story(tmp_path, run_ids=["r1"], scope_tags=["holdout"])
    assert [(e.ts, e.stream, e.kind, e.subject_id) for e in result.events] == [
        (e.ts, e.stream, e.kind, e.subject_id) for e in merged
    ]
