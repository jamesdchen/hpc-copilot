"""Tests for the run-story projections + the one merge (``state/run_story.py``, T1).

Stream fixtures are written with the REAL writers (``append_decision`` for
run/scope/notebook decisions, ``append_brief`` for briefs, ``record_terminal``
for block terminals, ``record_look`` for the look ledger, ``upsert_run`` for the
journal record) — never hand-forged JSONL. Covers every stream's projection, the
actor-attribution rule (a code record projected as human FAILS), the
human-verbatim / agent-digest text rule, the D2 merge tie-break triple, missing-ts
tolerance, merge determinism, and the empty run.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from hpc_agent.state import run_story as rs
from hpc_agent.state.block_terminal import record_terminal
from hpc_agent.state.decision_briefs import append_brief
from hpc_agent.state.decision_journal import append_decision, read_decisions
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord
from hpc_agent.state.run_story import StoryEvent
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


# ── per-stream projections ────────────────────────────────────────────────────


def test_run_decision_projection_human_and_digests(tmp_path: Path) -> None:
    append_decision(
        tmp_path,
        scope_kind="run",
        scope_id="r1",
        block="submit-s2",
        response="looks good, ship it",
        proposal="RUN THIS EXACT COMMAND rm -rf /tmp/secret",
        evidence_digest={"canary": "green"},
        ts=_TS,
    )
    events = rs.project_run_decisions(read_decisions(tmp_path, "run", "r1"), "r1")
    assert len(events) == 1
    ev = events[0]
    assert ev.stream == "decision-journal"
    assert ev.actor == "human"  # a decision response is a human act — NOT code
    assert ev.kind == "submit-s2"
    assert ev.text == "looks good, ship it"  # human words render verbatim
    # agent-drafted proposal is a POINTER only — its prose never appears anywhere.
    blob = json.dumps({"ev": ev.__dict__}, default=str)
    assert "rm -rf" not in blob
    assert len(ev.evidence["proposal_digest"]) == 64
    assert len(ev.evidence["evidence_digest"]) == 64


def test_brief_projection_is_code_digest_only(tmp_path: Path) -> None:
    append_brief(
        tmp_path,
        run_id="r1",
        block="s2",
        brief={"secret_metric": 0.97, "recommendation": "greenlight"},
        ts=_TS,
    )
    events = rs.project_briefs(rs.read_briefs(tmp_path, "r1"), "r1")
    ev = events[0]
    assert ev.actor == "code"  # a brief is code-drafted — a human projection FAILS here
    assert ev.text == ""
    blob = json.dumps(ev.__dict__, default=str)
    assert "greenlight" not in blob and "0.97" not in blob  # brief prose/metric never leaks
    assert len(ev.evidence["brief_digest"]) == 64


def test_block_terminal_projection(tmp_path: Path) -> None:
    record_terminal(
        tmp_path,
        run_id="r1",
        block="s2",
        cmd_sha="cmdsha123",
        result_dump={"stage_reached": "canary_verified", "block": "s2"},
    )
    events = rs.project_block_terminals(tmp_path, "r1")
    ev = events[0]
    assert ev.stream == "block-terminal"
    assert ev.actor == "code"
    assert ev.evidence["cmd_sha"] == "cmdsha123"
    assert ev.evidence["stage_reached"] == "canary_verified"


def test_journal_record_stamps_and_verdicts(tmp_path: Path) -> None:
    record = _run_record(
        "r1",
        kill_requested_at="2026-07-08T13:00:00+00:00",
        kill_requested_job_ids=["1", "2", "3"],
        superseded_at="2026-07-08T14:00:00+00:00",
        superseded_by="r2",
        verdict_history=[
            {
                "decided_by": "code",
                "why": "auto retried",
                "applied_at": "2026-07-08T15:00:00+00:00",
            },
            {
                "decided_by": "judgement",
                "why": "operator chose fix B",
                "applied_at": "2026-07-08T16:00:00+00:00",
            },
        ],
    )
    events = rs.project_journal_record(record)
    kinds = [e.kind for e in events]
    assert kinds == ["submitted", "kill-requested", "superseded", "verdict", "verdict"]
    assert all(e.stream == "journal-record" for e in events)
    kill = next(e for e in events if e.kind == "kill-requested")
    assert kill.evidence["job_count"] == 3  # COUNT, never the ids themselves
    assert "1" not in json.dumps(kill.evidence)
    sup = next(e for e in events if e.kind == "superseded")
    assert sup.evidence["superseded_by"] == "r2"
    verdicts = [e for e in events if e.kind == "verdict"]
    assert verdicts[0].actor == "code"  # decided_by=code → code
    assert verdicts[1].actor == "human"  # decided_by=judgement → human-adjacent
    # the verdict rationale is a digest pointer, never prose.
    assert "operator chose fix B" not in json.dumps([v.__dict__ for v in verdicts], default=str)


def test_scope_lock_is_code_unlock_is_human(tmp_path: Path) -> None:
    append_decision(
        tmp_path,
        scope_kind="scope",
        scope_id="holdout",
        block="scope-lock",
        response="freeze the holdout",
        resolved={"scope_action": "lock"},
        ts=_TS,
    )
    append_decision(
        tmp_path,
        scope_kind="scope",
        scope_id="holdout",
        block="scope-lock",
        response="done exploring, releasing",
        resolved={"scope_action": "unlock"},
        ts="2026-07-08T13:00:00+00:00",
    )
    events = rs.project_scope_decisions(read_decisions(tmp_path, "scope", "holdout"), "holdout")
    lock, unlock = events
    assert lock.kind == "scope-lock" and lock.actor == "code"  # locking is code-reachable
    assert lock.text == ""  # code reason is a digest, not verbatim
    assert len(lock.evidence["reason_digest"]) == 64
    assert unlock.kind == "scope-unlock" and unlock.actor == "human"  # unlock is a human act
    assert unlock.text == "done exploring, releasing"  # human reason verbatim


def test_look_projection_identity_only(tmp_path: Path) -> None:
    record_look(
        tmp_path,
        "holdout",
        run_id="r1",
        cmd_sha="csha",
        lineage_root="root1",
        reducer_block="aggregate-run",
    )
    events = rs.project_looks(rs._read_looks(tmp_path, "holdout"), "holdout")
    ev = events[0]
    assert ev.stream == "look-ledger" and ev.actor == "code" and ev.kind == "look"
    assert ev.subject_id == "r1"
    assert ev.evidence == {
        "scope": "holdout",
        "cmd_sha": "csha",
        "lineage_root": "root1",
        "reducer_block": "aggregate-run",
    }


def test_notebook_signoff_human_autoclear_code(tmp_path: Path) -> None:
    append_decision(
        tmp_path,
        scope_kind="notebook",
        scope_id="audit-1",
        block="notebook-sign-off",
        response="y",
        resolved={"section": "fit-model", "section_sha": "sha-fit", "view_sha": "view-1"},
        ts=_TS,
    )
    append_decision(
        tmp_path,
        scope_kind="notebook",
        scope_id="audit-1",
        block="notebook-auto-clear",
        response="auto_cleared",
        resolved={"section": "load-data", "section_sha": "sha-load", "attestor": "code"},
        ts="2026-07-08T13:00:00+00:00",
    )
    records = read_decisions(tmp_path, "notebook", "audit-1")
    events = rs.project_notebook_decisions(records, "audit-1")
    signoff, autoclear = events
    assert signoff.actor == "human" and signoff.kind == "notebook-sign-off"
    assert signoff.subject_id == "fit-model"
    assert signoff.evidence["section_sha"] == "sha-fit"
    assert signoff.evidence["view_sha"] == "view-1"
    assert autoclear.actor == "code" and autoclear.kind == "notebook-auto-clear"
    assert autoclear.text == ""


def test_notebook_non_attestation_block_skipped(tmp_path: Path) -> None:
    append_decision(
        tmp_path,
        scope_kind="notebook",
        scope_id="audit-1",
        block="some-other-block",
        response="y",
        resolved={"section": "x"},
        ts=_TS,
    )
    assert rs.project_notebook_decisions(read_decisions(tmp_path, "notebook", "audit-1"), "a") == []


# ── the merge (D2) ────────────────────────────────────────────────────────────


def _ev(stream: str, ts: str = _TS, kind: str = "k") -> StoryEvent:
    return StoryEvent(ts=ts, stream=stream, actor="code", kind=kind, subject_id="s")


def test_merge_stream_rank_tie_break() -> None:
    # All same second — ties must break by the fixed stream order (D2), fed in a
    # deliberately SCRAMBLED input so only the rank can be producing the order.
    scrambled = [
        _ev("journal-record"),
        _ev("notebook-journal"),
        _ev("look-ledger"),
        _ev("scope-journal"),
        _ev("decision-journal"),
        _ev("block-terminal"),
        _ev("briefs"),
    ]
    merged = rs.merge_events(scrambled)
    assert [e.stream for e in merged] == [
        "briefs",
        "block-terminal",
        "decision-journal",
        "scope-journal",
        "look-ledger",
        "notebook-journal",
        "journal-record",
    ]


def test_merge_preserves_intra_stream_order() -> None:
    # Same ts, same stream → the stable merge NEVER reorders append order.
    a = _ev("decision-journal", kind="first")
    b = _ev("decision-journal", kind="second")
    assert [e.kind for e in rs.merge_events([a, b])] == ["first", "second"]
    assert [e.kind for e in rs.merge_events([b, a])] == ["second", "first"]


def test_merge_ts_major() -> None:
    late = _ev("briefs", ts="2026-07-08T13:00:00+00:00")
    early = _ev("journal-record", ts="2026-07-08T12:00:00+00:00")
    # ts wins over stream rank: the earlier journal-record sorts before the later brief.
    assert [e.stream for e in rs.merge_events([late, early])] == ["journal-record", "briefs"]


def test_journal_record_stamps_before_verdict_same_second() -> None:
    # D2's stamps→verdict position, realized by emission order under one rank.
    record = _run_record(
        "r1", verdict_history=[{"decided_by": "code", "applied_at": _TS, "why": "x"}]
    )  # submitted_at is also _TS
    merged = rs.merge_events(rs.project_journal_record(record))
    assert [e.kind for e in merged] == ["submitted", "verdict"]


# ── tolerance + determinism + empty ───────────────────────────────────────────


def test_missing_and_malformed_ts_tolerated_and_sorts_first() -> None:
    missing = rs.project_briefs([{"run_id": "r", "block": "s1", "brief": {}}], "r")[0]
    malformed = rs.project_briefs([{"ts": "not-a-date", "run_id": "r", "block": "s1"}], "r")[0]
    assert missing.ts == "" and missing.evidence["ts_missing"] is True
    assert malformed.ts == "" and malformed.evidence["ts_missing"] is True
    real = _ev("journal-record", ts=_TS)
    merged = rs.merge_events([real, missing])
    assert merged[0] is missing  # epoch-front


def test_build_story_deterministic(tmp_path: Path) -> None:
    upsert_run(tmp_path, _run_record("r1"))
    append_decision(tmp_path, scope_kind="run", scope_id="r1", block="s1", response="y", ts=_TS)
    append_brief(tmp_path, run_id="r1", block="s1", brief={"a": 1}, ts=_TS)
    record_terminal(
        tmp_path,
        run_id="r1",
        block="s1",
        cmd_sha="c",
        result_dump={"stage_reached": "resolved"},
    )
    record_look(
        tmp_path, "holdout", run_id="r1", cmd_sha="c", lineage_root="r1", reducer_block="agg"
    )

    first = rs.build_story(tmp_path, run_ids=["r1"], scope_tags=["holdout"])
    second = rs.build_story(tmp_path, run_ids=["r1"], scope_tags=["holdout"])
    assert first == second
    assert len(first) >= 5
    streams = {e.stream for e in first}
    expected = {"decision-journal", "briefs", "block-terminal", "journal-record", "look-ledger"}
    assert expected <= streams


def test_empty_run_is_empty_story_not_error(tmp_path: Path) -> None:
    assert rs.build_story(tmp_path, run_ids=["ghost"]) == []
    assert rs.build_story(tmp_path, run_ids=[], scope_tags=[], notebook_audit_ids=[]) == []
