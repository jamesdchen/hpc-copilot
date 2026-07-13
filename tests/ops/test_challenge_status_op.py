"""T3 tests — ``ops/challenge_status_op.py``: the ``challenge-status`` query.

Toy WIDGET vocabulary only (never harxhar/quant — the domain-packs toy-fixture
rule). Both dependencies have landed: the op hard-imports the T2 wire models
(``_wire/queries/challenge_status.py``) and the T1 collector
(``state/challenges.py::standing_challenges``), so this suite constructs REAL
``ChallengeStatus`` / ``StandingChallenges`` rows and monkeypatches
``standing_challenges`` onto the op module (the ONE-collector seam) to feed them.

Exercises: the thread view; the target view (by content_sha, by subject pair);
a superseded target DISCLOSED (never raised); per-citation read-time re-resolution
(the E-read posture — unresolvable on an empty namespace, disclosed not refused);
render + view_sha byte-stability ×2; the no-interpretation vocabulary token pin;
the non-creating pin; fleet + skipped accounting; the exactly-one addressing spec
guard.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hpc_agent._wire.queries.challenge_status import (
    ChallengeStatusResult,
    ChallengeStatusSpec,
)
from hpc_agent.ops import challenge_status_op
from hpc_agent.ops.challenge_status_op import challenge_status
from hpc_agent.state.challenges import ChallengeStatus, StandingChallenges

_SHA_A = "aaaa1111bbbb2222"
_SHA_C = "cccc3333dddd4444"


# --- real T1 ``ChallengeStatus`` rows, built from toy WIDGET fixtures --------


def _status(
    challenge_id: str,
    *,
    status: str = "open",
    content_sha: str = _SHA_A,
    superseded: bool = False,
    filed_at: str = "2026-07-01T00:00:00+00:00",
    grounds: str = "the widget batch replication did not reproduce the row",
    subject_kind: str = "conclusion",
    subject_id: str = "widget-concl",
    kind: str = "attestation",
    verdict: str | None = None,
    reasoning: str | None = None,
    resolved_at: str | None = None,
    citations: list[dict[str, str]] | None = None,
) -> ChallengeStatus:
    """One reduced :class:`state.challenges.ChallengeStatus` — the real collector row."""
    target = {
        "kind": kind,
        "subject_kind": subject_kind,
        "subject_id": subject_id,
        "content_sha": content_sha,
        "scope": {"scope_kind": "conclusion", "scope_id": subject_id},
    }
    cits = (
        citations
        if citations is not None
        else [{"kind": "fingerprint", "ref": "widget-run-2", "sha": "f1c2b3a4deadbeef"}]
    )
    return ChallengeStatus(
        challenge_id=challenge_id,
        status="superseded" if superseded else status,
        target=target,
        filing={
            "challenge_id": challenge_id,
            "target": target,
            "citations": cits,
            "grounds": grounds,
            "content_sha": content_sha,
        },
        filed_at=filed_at,
        content_sha=content_sha,
        verdict=verdict,
        reasoning=reasoning,
        resolved_at=resolved_at,
        superseded=superseded,
    )


def _install_collector(monkeypatch: pytest.MonkeyPatch, statuses: list[ChallengeStatus]) -> None:
    """Monkeypatch ``standing_challenges`` to return *statuses*, address-filtered.

    Mirrors the pinned T1 filter (a :class:`StandingChallenges` bundle): with no
    address kwargs (the thread-view call) it returns everything; with
    ``content_sha`` / subject kwargs it narrows.
    """

    def _fake(experiment_dir: Path, **kw: Any) -> StandingChallenges:
        out = list(statuses)
        if kw.get("content_sha") is not None:
            out = [s for s in out if s.target and s.target["content_sha"] == kw["content_sha"]]
        if kw.get("subject_kind") is not None:
            out = [s for s in out if s.target and s.target["subject_kind"] == kw["subject_kind"]]
        if kw.get("subject_id") is not None:
            out = [s for s in out if s.target and s.target["subject_id"] == kw["subject_id"]]
        return StandingChallenges(
            experiment_dir=str(experiment_dir),
            statuses=tuple(out),
            contested=None,
            skipped=(),
        )

    monkeypatch.setattr(challenge_status_op, "standing_challenges", _fake)


def _run(exp: Path, **spec_kw: Any) -> ChallengeStatusResult:
    return challenge_status(experiment_dir=exp, spec=ChallengeStatusSpec(**spec_kw))


# --- the thread view ---------------------------------------------------------


def test_thread_view_selects_the_one_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exp = tmp_path / "exp"
    _install_collector(
        monkeypatch,
        [
            _status("widget-ch-1", status="open"),
            _status("widget-ch-2", status="dismissed", content_sha=_SHA_C),
        ],
    )
    res = _run(exp, challenge_id="widget-ch-1")
    assert isinstance(res, ChallengeStatusResult)
    assert [e.challenge_id for e in res.challenges] == ["widget-ch-1"]
    assert res.challenges[0].status == "open"
    assert res.challenges[0].resolution == "found-current"
    assert res.contested.open == 1 and res.contested.challenge_ids == ["widget-ch-1"]
    assert res.render.splitlines()[1].startswith("thread · challenge widget-ch-1")
    # Per-citation read-time re-resolution is DISCLOSED: the toy fingerprint
    # citation is unresolvable on the empty namespace (never raised — E-read).
    assert [c.challenge_id for c in res.citations_status] == ["widget-ch-1"]
    assert res.citations_status[0].kind == "fingerprint"
    assert res.citations_status[0].verified is False


# --- the target view ---------------------------------------------------------


def test_target_view_by_content_sha(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exp = tmp_path / "exp"
    _install_collector(
        monkeypatch,
        [
            _status("widget-ch-1", status="open"),
            _status(
                "widget-ch-3",
                status="upheld",
                verdict="upheld",
                reasoning="the widget row is refuted",
                resolved_at="2026-07-02T00:00:00+00:00",
            ),
            _status("widget-ch-2", status="open", content_sha=_SHA_C),
        ],
    )
    res = _run(exp, content_sha=_SHA_A)
    assert {e.challenge_id for e in res.challenges} == {"widget-ch-1", "widget-ch-3"}
    assert res.contested.open == 1 and res.contested.upheld == 1
    upheld = next(e for e in res.challenges if e.challenge_id == "widget-ch-3")
    assert upheld.verdict is not None
    assert upheld.verdict.verdict == "upheld"
    assert upheld.verdict.reasoning == "the widget row is refuted"
    assert res.render.splitlines()[1].startswith("target · content_sha")


def test_target_view_by_subject_pair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exp = tmp_path / "exp"
    _install_collector(monkeypatch, [_status("widget-ch-1", status="open")])
    res = _run(exp, subject_kind="conclusion", subject_id="widget-concl")
    assert [e.challenge_id for e in res.challenges] == ["widget-ch-1"]
    assert res.challenges[0].target.subject_kind == "conclusion"
    assert res.challenges[0].target.subject_id == "widget-concl"


# --- superseded target disclosed, never refused ------------------------------


def test_superseded_target_disclosed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exp = tmp_path / "exp"
    _install_collector(
        monkeypatch,
        [_status("widget-ch-7", superseded=True, content_sha="0ldsha0000000000")],
    )
    # The read DISCLOSES the moved target; it must NOT raise (only the append
    # gate refuses).
    res = _run(exp, challenge_id="widget-ch-7")
    assert res.challenges[0].resolution == "found-superseded"
    assert res.contested.superseded == 1


# --- render + view_sha byte-stability ×2 -------------------------------------


def test_render_and_view_sha_byte_stable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exp = tmp_path / "exp"
    statuses = [
        _status("widget-ch-1", status="open"),
        _status(
            "widget-ch-3",
            status="upheld",
            verdict="upheld",
            reasoning="refuted",
            resolved_at="2026-07-02T00:00:00+00:00",
        ),
    ]
    _install_collector(monkeypatch, statuses)
    r1 = _run(exp, content_sha=_SHA_A)
    r2 = _run(exp, content_sha=_SHA_A)
    # No wall-clock in the render/view_sha → both are byte-identical across calls
    # (computed_at is EXCLUDED from the projection the view_sha shas over).
    assert r1.render == r2.render
    assert r1.view_sha == r2.view_sha
    assert len(r1.view_sha) == 64  # sha-256 hex
    assert r1.render.startswith("# challenge-status")


def test_view_sha_tracks_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exp = tmp_path / "exp"
    _install_collector(monkeypatch, [_status("widget-ch-1", status="open")])
    r_open = _run(exp, content_sha=_SHA_A)
    _install_collector(
        monkeypatch,
        [
            _status(
                "widget-ch-1",
                status="upheld",
                verdict="upheld",
                reasoning="refuted",
                resolved_at="2026-07-02T00:00:00+00:00",
            )
        ],
    )
    r_upheld = _run(exp, content_sha=_SHA_A)
    # A different reduced status → a different projection → a different view_sha.
    assert r_open.view_sha != r_upheld.view_sha


# --- the no-interpretation vocabulary token pin ------------------------------

_FORBIDDEN_TOKENS = (
    "should",
    "recommend",
    "urgent",
    "critical",
    "warning",
    "immediately",
    "must ",
    "please",
    "danger",
    "severe",
    "priority",
)


def test_no_interpretation_vocabulary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exp = tmp_path / "exp"
    _install_collector(
        monkeypatch,
        [
            _status("widget-ch-1", status="open"),
            _status(
                "widget-ch-2",
                status="dismissed",
                verdict="dismissed",
                reasoning="the envelope covers the delta",
                resolved_at="2026-07-02T00:00:00+00:00",
            ),
        ],
    )
    res = _run(exp, content_sha=_SHA_A)
    body = res.render.lower()
    for token in _FORBIDDEN_TOKENS:
        assert token not in body, f"render leaked interpretation vocabulary: {token!r}"


# --- the non-creating pin ----------------------------------------------------


def test_non_creating(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A read over a fresh experiment dir materializes NO directories."""
    exp = tmp_path / "exp"
    exp.mkdir()
    _install_collector(monkeypatch, [])
    res = _run(exp, challenge_id="widget-ch-1")
    assert res.challenges == []
    assert res.contested.open == 0 and res.contested.challenge_ids == []
    assert list(exp.iterdir()) == []  # no .hpc tree scaffolded


# --- fleet + skipped accounting ----------------------------------------------


def test_fleet_with_skipped_namespace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exp1 = tmp_path / "exp1"
    exp2 = tmp_path / "exp2"

    monkeypatch.setattr(
        challenge_status_op,
        "discover_fleet_experiments",
        lambda: ([exp1, exp2], [{"ref": "torn", "reason": "unreadable/torn repo.json"}]),
    )

    def _fake(experiment_dir: Path, **kw: Any) -> StandingChallenges:
        who = "widget-ch-a" if experiment_dir == exp1 else "widget-ch-b"
        return StandingChallenges(
            experiment_dir=str(experiment_dir),
            statuses=(_status(who, status="open"),),
            contested=None,
            skipped=(),
        )

    monkeypatch.setattr(challenge_status_op, "standing_challenges", _fake)

    res = _run(tmp_path / "unused", content_sha=_SHA_A, fleet=True)
    assert {e.challenge_id for e in res.challenges} == {"widget-ch-a", "widget-ch-b"}
    assert [s.ref for s in res.skipped] == ["torn"]
    assert res.contested.open == 2


# --- the exactly-one addressing spec guard (the real wire spec) --------------


def test_spec_requires_exactly_one_address() -> None:
    with pytest.raises(ValueError):
        ChallengeStatusSpec()  # no address
    with pytest.raises(ValueError):
        ChallengeStatusSpec(challenge_id="widget-ch-1", content_sha=_SHA_A)  # two
    with pytest.raises(ValueError):
        ChallengeStatusSpec(subject_kind="conclusion")  # a bare half of the subject pair
