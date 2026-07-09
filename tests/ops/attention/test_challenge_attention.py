"""T7 — the challenge attention edges (``ops/attention_queue.py``, C-queue).

New item kinds ``challenge-open`` (a pending human verdict → VERDICT) and
``challenge-upheld-unremedied`` (a standing refutation nothing has answered →
INFORMATIONAL), plus the leverage fan-out: an open challenge's ``unblocks`` grows
by the live registrations whose prerequisite chains name the contested
``content_sha`` (the R8 edge). The predicate routes through the ONE reduction
``state/challenges.py::standing_challenges`` (the D5 route-through pin), never a
re-read of a challenge journal.

TOY VOCABULARY ONLY: widget lineage. Never harxhar/quant words.
"""

from __future__ import annotations

import inspect
import json
from typing import TYPE_CHECKING

from hpc_agent.ops import attention_queue as aq
from hpc_agent.state.decision_journal import append_decision as _state_append

if TYPE_CHECKING:
    from pathlib import Path

_NOW = "2026-07-09T00:00:00Z"
_CID = "widget-dissent-1"
_TARGET_SHA = "a" * 64  # the challenged (and prereq-bound) content_sha


def _filing(cid: str = _CID, *, content_sha: str = _TARGET_SHA) -> dict:
    return {
        "ts": "2026-07-01T00:00:00Z",
        "block": "challenge",
        "resolved": {
            "challenge_id": cid,
            "target": {
                "kind": "run",
                "subject_kind": "conclusion",
                "subject_id": "widget-c1",
                "content_sha": content_sha,
                "scope": {"scope_kind": "conclusion", "scope_id": "widget-c1"},
            },
            "citations": [{"kind": "run", "ref": "widget-run-1", "sha": "d" * 64}],
            "grounds": "widget conclusion rests on a run that failed replication",
        },
    }


def _write_challenge(experiment_dir: Path, cid: str, *records: dict) -> None:
    p = experiment_dir / ".hpc" / "challenges" / f"{cid}.decisions.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _verdict(cid: str = _CID, *, verdict: str = "upheld") -> dict:
    return {
        "ts": "2026-07-05T00:00:00Z",
        "block": "challenge-verdict",
        "resolved": {"challenge_id": cid, "verdict": verdict, "reasoning": "the run does not hold"},
    }


def _withdraw(cid: str = _CID) -> dict:
    return {
        "ts": "2026-07-05T00:00:00Z",
        "block": "challenge-withdraw",
        "resolved": {"challenge_id": cid, "reason": "re-checked; my read was wrong"},
    }


def test_open_challenge_surfaces_as_verdict_item(tmp_path: Path) -> None:
    _write_challenge(tmp_path, _CID, _filing())
    items = aq.collect_challenges(tmp_path, now=_NOW)
    assert len(items) == 1
    item = items[0]
    assert item.kind == aq.CHALLENGE_OPEN
    assert item.item_class == aq.VERDICT
    assert item.scope_kind == "challenge"
    assert item.scope_id == _CID
    assert item.since == "2026-07-01T00:00:00Z"  # the filing ts (the item ages)
    assert item.evidence["content_sha"] == _TARGET_SHA


def test_upheld_unremedied_surfaces_as_informational(tmp_path: Path) -> None:
    _write_challenge(tmp_path, _CID, _filing(), _verdict(verdict="upheld"))
    items = aq.collect_challenges(tmp_path, now=_NOW)
    assert len(items) == 1
    assert items[0].kind == aq.CHALLENGE_UPHELD_UNREMEDIED
    assert items[0].item_class == aq.INFORMATIONAL


def test_dismissed_and_withdrawn_yield_no_item(tmp_path: Path) -> None:
    _write_challenge(
        tmp_path,
        "widget-dismissed",
        _filing("widget-dismissed"),
        _verdict("widget-dismissed", verdict="dismissed"),
    )
    _write_challenge(
        tmp_path, "widget-withdrawn", _filing("widget-withdrawn"), _withdraw("widget-withdrawn")
    )
    assert aq.collect_challenges(tmp_path, now=_NOW) == []


def test_fanout_counts_registrations_naming_the_contested_sha(tmp_path: Path) -> None:
    """The R8 edge: an open challenge unblocks the live registrations binding its sha."""
    _write_challenge(tmp_path, _CID, _filing())
    _state_append(
        tmp_path,
        scope_kind="registration",
        scope_id="reg-widgets",
        block="registration",
        response="register reg-widgets",
        resolved={
            "registration_id": "reg-widgets",
            "run_id": "widget-run-1",
            "dossier_sha": "d" * 64,
            "prerequisites": [
                {
                    "slot": "repro",
                    "kind": "reproduction",
                    "subject_id": "widget-run-1",
                    "content_sha": _TARGET_SHA,
                }
            ],
        },
    )
    collection = aq.collect_items(tmp_path, now=_NOW)
    opens = [i for i in collection.items if i.kind == aq.CHALLENGE_OPEN]
    assert len(opens) == 1
    assert opens[0].unblocks == 1  # the one live registration binding the contested sha


def test_fanout_zero_when_no_registration_names_the_sha(tmp_path: Path) -> None:
    _write_challenge(tmp_path, _CID, _filing())
    collection = aq.collect_items(tmp_path, now=_NOW)
    opens = [i for i in collection.items if i.kind == aq.CHALLENGE_OPEN]
    assert len(opens) == 1
    assert opens[0].unblocks == 0  # no encoded edge → falls through to class order


def test_fail_open_on_unreadable_store(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    def _boom(experiment_dir, **kw):  # noqa: ANN001, ANN202
        raise RuntimeError("challenge store unreadable")

    monkeypatch.setattr("hpc_agent.state.challenges.standing_challenges", _boom)
    assert aq.collect_challenges(tmp_path, now=_NOW) == []


def test_route_through_standing_challenges() -> None:
    """The D5 pin: the collector routes through standing_challenges, never re-reads."""
    src = inspect.getsource(aq.collect_challenges)
    assert "standing_challenges(" in src
