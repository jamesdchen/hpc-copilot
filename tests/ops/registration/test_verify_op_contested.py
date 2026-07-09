"""T6 — the C-disclose ``contested`` seam on ``verify-registration``.

The capital-boundary disclosure seat (challenge-attestation C-disclose): the
registration's own standing challenges + one block per contested prerequisite
slot, routed through the ONE collector ``state/challenges.py::standing_challenges``
(the C-disclose route-through enforcement row). DISCLOSED, never blocking (C4);
orthogonal to ``status`` (C-status); kept OUT of ``view_sha`` (R6 — a later
challenge must not drift a bound witness). All-zero omits the block; fail-open.

Reuses the T5-reporter harness (``_read_records`` / ``_check_chain`` /
``compute_dossier_signature`` stubbed) and additionally stubs
``verify_op.standing_challenges`` so the seat is exercised without depending on
real challenge journals.

TOY VOCABULARY ONLY: widget lineage.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent.ops.registration import verify_op
from hpc_agent.state.challenges import Contested, StandingChallenges

# Reuse the T5 reporter harness verbatim.
from tests.ops.registration.test_verify_op import (
    _FakeVerdict,
    _install,
    _registration_record,
    _verify,
    _write_template,
)

if TYPE_CHECKING:
    from pathlib import Path

_PREREQ_SHA = "c" * 64
_PREREQS = [
    {
        "slot": "repro",
        "kind": "reproduction",
        "subject_id": "widget-run-1",
        "content_sha": _PREREQ_SHA,
    }
]
_VERDICTS = [
    _FakeVerdict(
        slot="repro",
        kind="reproduction",
        status="current",
        recorded_sha=_PREREQ_SHA,
        recomputed_sha=_PREREQ_SHA,
        evidence_note="ok",
    )
]


def _contested(open_: int, *ids: str) -> Contested:
    return Contested(
        open=open_,
        upheld=0,
        dismissed=0,
        withdrawn=0,
        superseded=0,
        challenge_ids=tuple(ids),
    )


def _standing(block_for: dict[str, Contested | None]):
    """A ``standing_challenges`` stub keyed on the address kwargs.

    ``subject_kind='registration'`` → the registration's own block; ``content_sha``
    → the per-slot block. Returns a real :class:`StandingChallenges` wrapper.
    """

    def _fake(experiment_dir: Any, **kw: Any) -> StandingChallenges:
        if kw.get("subject_kind") == "registration":
            block = block_for.get("registration")
        elif kw.get("content_sha") is not None:
            block = block_for.get(str(kw["content_sha"]))
        else:
            block = None
        return StandingChallenges(
            experiment_dir=str(experiment_dir), statuses=(), contested=block, skipped=()
        )

    return _fake


def _install_reg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write_template(tmp_path)
    _install(
        monkeypatch,
        records=[_registration_record(prerequisites=_PREREQS)],
        verdicts=_VERDICTS,
    )


def test_contested_blocks_attach_and_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_reg(monkeypatch, tmp_path)
    monkeypatch.setattr(
        verify_op,
        "standing_challenges",
        _standing(
            {
                "registration": _contested(2, "widget-reg-dissent-a", "widget-reg-dissent-b"),
                _PREREQ_SHA: _contested(1, "widget-repro-dissent"),
            }
        ),
    )
    res = _verify(tmp_path, registration_id="reg-widgets")

    # DISCLOSED beside the status (never blocking, orthogonal): still current.
    assert res.status == "current"
    assert res.contested is not None
    assert res.contested.open == 2
    assert list(res.contested.challenge_ids) == ["widget-reg-dissent-a", "widget-reg-dissent-b"]
    assert len(res.prerequisite_contested) == 1
    assert res.prerequisite_contested[0].slot == "repro"
    assert res.prerequisite_contested[0].contested.open == 1
    assert "## Contested" in res.brief
    assert "widget-repro-dissent" in res.brief


def test_uncontested_omits_the_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_reg(monkeypatch, tmp_path)
    monkeypatch.setattr(verify_op, "standing_challenges", _standing({}))
    res = _verify(tmp_path, registration_id="reg-widgets")

    assert res.status == "current"
    assert res.contested is None
    assert res.prerequisite_contested == []
    assert "## Contested" not in res.brief


def test_view_sha_unperturbed_by_contest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The R6 invariant: contest is OUTSIDE view_sha — a challenge cannot drift it."""
    _install_reg(monkeypatch, tmp_path)
    monkeypatch.setattr(verify_op, "standing_challenges", _standing({}))
    clean = _verify(tmp_path, registration_id="reg-widgets")

    monkeypatch.setattr(
        verify_op,
        "standing_challenges",
        _standing({"registration": _contested(3, "a", "b", "c"), _PREREQ_SHA: _contested(1, "d")}),
    )
    contested = _verify(tmp_path, registration_id="reg-widgets")

    assert contested.view_sha == clean.view_sha  # the witness is unmoved
    assert contested.contested is not None  # but the disclosure IS present


def test_fail_open_when_collector_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_reg(monkeypatch, tmp_path)

    def _boom(experiment_dir: Any, **kw: Any) -> StandingChallenges:
        raise RuntimeError("challenge store unreadable")

    monkeypatch.setattr(verify_op, "standing_challenges", _boom)
    res = _verify(tmp_path, registration_id="reg-widgets")

    # A disclosure gap never breaks the report and never blocks.
    assert res.status == "current"
    assert res.contested is None
    assert res.prerequisite_contested == []


def test_route_through_the_one_collector(tmp_path: Path) -> None:
    """C-disclose enforcement row: the seat calls ``standing_challenges``, no re-glob."""
    src = inspect.getsource(verify_op._attach_contested) + inspect.getsource(
        verify_op._contested_counts
    )
    assert "standing_challenges(" in src


def _write_challenge_filing(
    exp: Path, challenge_id: str, *, subject_kind: str, subject_id: str, content_sha: str
) -> None:
    """Write a REAL, well-formed ``challenge`` filing the live collector accepts."""
    import json

    rec = {
        "ts": "2026-07-08T02:00:00Z",
        "block": "challenge",
        "resolved": {
            "challenge_id": challenge_id,
            "target": {
                "kind": "attestation",
                "subject_kind": subject_kind,
                "subject_id": subject_id,
                "content_sha": content_sha,
                "scope": {"scope_kind": "registration", "scope_id": subject_id},
            },
            "citations": [{"kind": "run", "ref": "widget-run-1", "sha": "d" * 64}],
            "grounds": "widget batch rests on a run that failed replication",
        },
    }
    p = exp / ".hpc" / "challenges" / f"{challenge_id}.decisions.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec) + "\n", encoding="utf-8")


def test_real_challenge_journal_surfaces_contested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end through the LIVE ``standing_challenges`` — no stub.

    A real ``.hpc/challenges`` filing targeting the registration reads ``open`` (the
    registration journal is absent, so the target never re-resolves as superseded)
    and surfaces on the ``verify-registration`` result + brief.
    """
    _install_reg(monkeypatch, tmp_path)  # stubs the reg reader/checker, NOT the collector
    _write_challenge_filing(
        tmp_path,
        "widget-reg-real-dissent",
        subject_kind="registration",
        subject_id="reg-widgets",
        content_sha="e" * 64,
    )
    res = _verify(tmp_path, registration_id="reg-widgets")

    assert res.status == "current"  # DISCLOSED, never blocking
    assert res.contested is not None
    assert res.contested.open == 1
    assert list(res.contested.challenge_ids) == ["widget-reg-real-dissent"]
    assert "## Contested" in res.brief
    assert "widget-reg-real-dissent" in res.brief
