"""T10 — the ``campaign-unconcluded`` attention collector (E-queue).

A TERMINAL campaign that no CURRENT conclusion names surfaces as an AGING,
INFORMATIONAL standing item (fan-out 0, aging by the completion ts). The predicate
routes through ``state/evidence.py::collect_evidence``'s ``unconcluded`` reduction
(the D5 route-through pin), never a re-inlined join.

TOY VOCABULARY ONLY: widget campaigns, never a real domain's words.
"""

from __future__ import annotations

import inspect
import json
from typing import TYPE_CHECKING

from hpc_agent.ops import attention_queue as aq

if TYPE_CHECKING:
    from pathlib import Path

_NOW = "2025-11-20T00:00:00Z"
_CAMP = "widget-camp-1"


def _seed_terminal_campaign(experiment_dir: Path, campaign_id: str = _CAMP) -> None:
    """Write a campaign journal ending in a ``complete`` block (a terminal campaign)."""
    cdir = experiment_dir / ".hpc" / "campaigns" / campaign_id
    cdir.mkdir(parents=True, exist_ok=True)
    lines = [
        {"ts": "2025-11-01T00:00:00Z", "block": "greenlight", "response": "y"},
        {"ts": "2025-11-14T00:00:00Z", "block": "complete", "response": "done"},
    ]
    (cdir / "decisions.jsonl").write_text(
        "\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8"
    )


def _seed_conclusion_naming(experiment_dir: Path, campaign_id: str = _CAMP) -> None:
    """Write a current conclusion whose ``concludes`` names the campaign."""
    conc_dir = experiment_dir / ".hpc" / "conclusions"
    conc_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": "2025-11-15T00:00:00Z",
        "scope_kind": "conclusion",
        "scope_id": "widget-done",
        "block": "conclusion",
        "response": "conclude widget-done — deadbeef01",
        "resolved": {
            "conclusion_id": "widget-done",
            "tags": ["edge-x"],
            "concludes": [{"scope_kind": "campaign", "scope_id": campaign_id}],
            "citations": [{"kind": "run", "ref": "widget-run-0", "sha": "deadbeef0100"}],
            "finding": "no alpha",
        },
    }
    (conc_dir / "widget-done.decisions.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )


def test_terminal_campaign_with_no_conclusion_surfaces(tmp_path: Path) -> None:
    _seed_terminal_campaign(tmp_path)
    items = aq.collect_campaign_unconcluded(tmp_path, now=_NOW)
    assert len(items) == 1
    item = items[0]
    assert item.kind == aq.CAMPAIGN_UNCONCLUDED
    assert item.item_class == aq.INFORMATIONAL
    assert item.scope_kind == "campaign"
    assert item.scope_id == _CAMP
    assert item.since == "2025-11-14T00:00:00Z"  # the completion ts (aging)
    assert item.unblocks == 0  # a missing conclusion blocks nothing (E3)
    assert item.action is None  # no prose beyond the identity line


def test_concluded_campaign_does_not_surface(tmp_path: Path) -> None:
    _seed_terminal_campaign(tmp_path)
    _seed_conclusion_naming(tmp_path)
    assert aq.collect_campaign_unconcluded(tmp_path, now=_NOW) == []


def test_non_terminal_campaign_does_not_surface(tmp_path: Path) -> None:
    cdir = tmp_path / ".hpc" / "campaigns" / _CAMP
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "decisions.jsonl").write_text(
        json.dumps({"ts": "2025-11-01T00:00:00Z", "block": "greenlight", "response": "y"}) + "\n",
        encoding="utf-8",
    )
    assert aq.collect_campaign_unconcluded(tmp_path, now=_NOW) == []


def test_fan_out_stays_zero_after_full_collect(tmp_path: Path) -> None:
    """Through the full collect_items + fan-out walk, the item stays leverage-zero."""
    _seed_terminal_campaign(tmp_path)
    collection = aq.collect_items(tmp_path, now=_NOW)
    unconc = [i for i in collection.items if i.kind == aq.CAMPAIGN_UNCONCLUDED]
    assert len(unconc) == 1
    assert unconc[0].unblocks == 0


def test_route_through_collect_evidence_unconcluded() -> None:
    """The D5 pin: the collector routes through collect_evidence, never re-joining."""
    src = inspect.getsource(aq.collect_campaign_unconcluded)
    assert "collect_evidence(" in src
    assert ".unconcluded" in src
