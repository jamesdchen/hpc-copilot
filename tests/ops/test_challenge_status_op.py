"""T3 tests — ``ops/challenge_status_op.py``: the ``challenge-status`` query.

Toy WIDGET vocabulary only (never harxhar/quant — the domain-packs toy-fixture
rule). T1 (``state/challenges.py::standing_challenges`` / ``contested_projection``)
and T2 (``_wire/queries/challenge_status.py``) are built by PARALLEL agents and
are ABSENT in this worktree; the op guards their imports and this suite STUBS
them: ``standing_challenges`` / ``contested_projection`` are monkeypatched onto
the op module, and the wire models are the op's own faithful placeholders
(``challenge_status_op.ChallengeStatusSpec`` / ``.ChallengeStatusResult``), which
the real T2 module shadows at merge.

Exercises: the thread view; the target view; an unresolvable target DISCLOSED
(never raised); render + view_sha byte-stability ×2; the no-interpretation
vocabulary token pin; the non-creating pin; fleet + skipped accounting; the
exactly-one addressing spec guard.

Registry expected-red: the op adds ``challenge-status`` (registry +1) with a
PLACEHOLDER spec_model and NO ``schemas/challenge_status.input.json`` — so the
registry-count pin and the schema-roundtrip tests are RED until the orchestrator
regen runs (deliberately NOT chased here; the task defers regen, +1 registry
debt).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hpc_agent.ops import challenge_status_op
from hpc_agent.ops.challenge_status_op import (
    ChallengeStatusResult,
    ChallengeStatusSpec,
    challenge_status,
)

_SHA_A = "aaaa1111bbbb2222"
_SHA_C = "cccc3333dddd4444"


# --- the pinned T1 entry contract, as toy stubs ------------------------------


def _cit(kind: str, ref: str, sha: str, *, verified: bool) -> SimpleNamespace:
    return SimpleNamespace(kind=kind, ref=ref, sha=sha, verified=verified)


def _entry(
    challenge_id: str,
    *,
    status: str = "open",
    content_sha: str = _SHA_A,
    resolution: str = "found-current",
    filed_ts: str = "2026-07-01T00:00:00+00:00",
    grounds: str = "the widget batch replication did not reproduce the row",
    subject_kind: str = "conclusion",
    subject_id: str = "widget-concl",
    kind: str = "attestation",
    verdict: str | None = None,
    reasoning: str | None = None,
    citations: list[SimpleNamespace] | None = None,
) -> SimpleNamespace:
    """One ``standing_challenges`` entry mirroring the pinned T1 attribute shape."""
    return SimpleNamespace(
        challenge_id=challenge_id,
        status=status,
        filed_ts=filed_ts,
        grounds=grounds,
        target=SimpleNamespace(
            kind=kind,
            subject_kind=subject_kind,
            subject_id=subject_id,
            content_sha=content_sha,
        ),
        target_resolution=resolution,
        verdict=verdict,
        reasoning=reasoning,
        citations=citations
        if citations is not None
        else [_cit("fingerprint", "widget-run-2", "f1c2b3a4deadbeef", verified=True)],
    )


def _contested_tally(entries: list[Any]) -> dict[str, Any]:
    """A faithful stand-in for T1's ``contested_projection`` (counts + ids)."""
    keys = ("open", "upheld", "dismissed", "withdrawn", "superseded")
    counts: dict[str, Any] = {k: sum(1 for e in entries if e.status == k) for k in keys}
    counts["challenge_ids"] = [e.challenge_id for e in entries]
    return counts


@pytest.fixture(autouse=True)
def _stub_t1(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wire the contested projection to the toy tally for every test."""
    monkeypatch.setattr(challenge_status_op, "contested_projection", _contested_tally)


def _install_collector(monkeypatch: pytest.MonkeyPatch, entries: list[SimpleNamespace]) -> None:
    """Monkeypatch ``standing_challenges`` to return *entries*, address-filtered.

    Mirrors the pinned T1 filter: with no address kwargs (the thread-view call)
    it returns everything; with ``content_sha`` / subject kwargs it narrows.
    """

    def _fake(experiment_dir: Path, **kw: Any) -> list[SimpleNamespace]:
        out = list(entries)
        if kw.get("content_sha") is not None:
            out = [e for e in out if e.target.content_sha == kw["content_sha"]]
        if kw.get("subject_kind") is not None:
            out = [e for e in out if e.target.subject_kind == kw["subject_kind"]]
        if kw.get("subject_id") is not None:
            out = [e for e in out if e.target.subject_id == kw["subject_id"]]
        return out

    monkeypatch.setattr(challenge_status_op, "standing_challenges", _fake)


def _run(exp: Path, **spec_kw: Any) -> ChallengeStatusResult:
    return challenge_status(experiment_dir=exp, spec=ChallengeStatusSpec(**spec_kw))


# --- the thread view ---------------------------------------------------------


def test_thread_view_selects_the_one_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exp = tmp_path / "exp"
    _install_collector(
        monkeypatch,
        [
            _entry("widget-ch-1", status="open"),
            _entry("widget-ch-2", status="dismissed", content_sha=_SHA_C),
        ],
    )
    res = _run(exp, challenge_id="widget-ch-1")
    assert isinstance(res, ChallengeStatusResult)
    assert res.view == "thread"
    assert res.addressed_challenge_id == "widget-ch-1"
    assert [e.challenge_id for e in res.entries] == ["widget-ch-1"]
    assert res.entries[0].status == "open"
    assert res.target_resolution == "found-current"
    assert res.contested.open == 1 and res.contested.challenge_ids == ["widget-ch-1"]
    # citation re-resolution disclosed (verified True from the stub).
    assert res.entries[0].citations[0].verified is True


# --- the target view ---------------------------------------------------------


def test_target_view_by_content_sha(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exp = tmp_path / "exp"
    _install_collector(
        monkeypatch,
        [
            _entry("widget-ch-1", status="open"),
            _entry(
                "widget-ch-3",
                status="upheld",
                verdict="upheld",
                reasoning="the widget row is refuted",
            ),
            _entry("widget-ch-2", status="open", content_sha=_SHA_C),
        ],
    )
    res = _run(exp, content_sha=_SHA_A)
    assert res.view == "target"
    assert res.addressed_content_sha == _SHA_A
    assert {e.challenge_id for e in res.entries} == {"widget-ch-1", "widget-ch-3"}
    assert res.contested.open == 1 and res.contested.upheld == 1
    upheld = next(e for e in res.entries if e.challenge_id == "widget-ch-3")
    assert upheld.verdict == "upheld" and upheld.reasoning == "the widget row is refuted"


def test_target_view_by_subject_pair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exp = tmp_path / "exp"
    _install_collector(monkeypatch, [_entry("widget-ch-1", status="open")])
    res = _run(exp, subject_kind="conclusion", subject_id="widget-concl")
    assert res.view == "target"
    assert res.addressed_subject_kind == "conclusion"
    assert res.addressed_subject_id == "widget-concl"
    assert [e.challenge_id for e in res.entries] == ["widget-ch-1"]


# --- unresolvable / superseded target disclosed, never refused ---------------


def test_unresolvable_target_disclosed_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exp = tmp_path / "exp"
    _install_collector(
        monkeypatch,
        [_entry("widget-ch-9", content_sha="deaddeaddeaddead", resolution="unresolvable")],
    )
    # The read DISCLOSES; it must NOT raise (only the append gate refuses).
    res = _run(exp, content_sha="deaddeaddeaddead")
    assert res.target_resolution == "unresolvable"
    assert res.entries[0].target_resolution == "unresolvable"


def test_superseded_target_disclosed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exp = tmp_path / "exp"
    _install_collector(
        monkeypatch,
        [
            _entry(
                "widget-ch-7",
                status="superseded",
                content_sha="0ldsha0000000000",
                resolution="found-superseded",
            )
        ],
    )
    res = _run(exp, challenge_id="widget-ch-7")
    assert res.target_resolution == "found-superseded"
    assert res.contested.superseded == 1


# --- render + view_sha byte-stability ×2 -------------------------------------


def test_render_and_view_sha_byte_stable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exp = tmp_path / "exp"
    entries = [
        _entry("widget-ch-1", status="open"),
        _entry("widget-ch-3", status="upheld", verdict="upheld", reasoning="refuted"),
    ]
    _install_collector(monkeypatch, entries)
    r1 = _run(exp, content_sha=_SHA_A)
    r2 = _run(exp, content_sha=_SHA_A)
    # No wall-clock anywhere → render + view_sha are byte-identical across calls.
    assert r1.render == r2.render
    assert r1.view_sha == r2.view_sha
    assert len(r1.view_sha) == 64  # sha-256 hex
    assert r1.render.startswith("# challenge-status")


def test_view_sha_tracks_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exp = tmp_path / "exp"
    _install_collector(monkeypatch, [_entry("widget-ch-1", status="open")])
    r_open = _run(exp, content_sha=_SHA_A)
    _install_collector(
        monkeypatch,
        [_entry("widget-ch-1", status="upheld", verdict="upheld", reasoning="refuted")],
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
            _entry("widget-ch-1", status="open"),
            _entry(
                "widget-ch-2",
                status="dismissed",
                verdict="dismissed",
                reasoning="the envelope covers the delta",
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
    assert res.entries == []
    assert res.target_resolution is None
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

    def _fake(experiment_dir: Path, **kw: Any) -> list[SimpleNamespace]:
        who = "widget-ch-a" if experiment_dir == exp1 else "widget-ch-b"
        return [_entry(who, status="open")]

    monkeypatch.setattr(challenge_status_op, "standing_challenges", _fake)

    res = _run(tmp_path / "unused", content_sha=_SHA_A, fleet=True)
    assert {e.challenge_id for e in res.entries} == {"widget-ch-a", "widget-ch-b"}
    assert [s.ref for s in res.skipped] == ["torn"]
    assert res.contested.open == 2


# --- the exactly-one addressing spec guard (T2 placeholder) ------------------


def test_spec_requires_exactly_one_address() -> None:
    with pytest.raises(ValueError):
        ChallengeStatusSpec()  # no address
    with pytest.raises(ValueError):
        ChallengeStatusSpec(challenge_id="widget-ch-1", content_sha=_SHA_A)  # two
    with pytest.raises(ValueError):
        ChallengeStatusSpec(subject_kind="conclusion")  # a bare half of the subject pair
