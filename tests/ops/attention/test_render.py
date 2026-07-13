"""Tests for the deterministic attention-queue markdown renderer (Wave B / T3).

The renderer is pure string work (D6): same ordered items + same ``computed_at``
→ byte-identical markdown, one ``computed_at`` stamp, ages rendered relative to it.
"""

from __future__ import annotations

import random

from hpc_agent.ops.attention_queue import (
    ALERT,
    BLOCKED,
    INFORMATIONAL,
    RUN_ANOMALY,
    RUN_STALLED,
    VERDICT,
    AttentionItem,
    order_items,
)
from hpc_agent.ops.attention_render import render_queue

_NOW = "2026-07-06T12:00:00+00:00"


def _item(kind: str, klass: str, scope_id: str, since: str | None, **kw: object) -> AttentionItem:
    return AttentionItem(
        kind=kind,
        item_class=klass,
        experiment_dir="/exp",
        scope_kind="run",
        scope_id=scope_id,
        since=since,
        **kw,  # type: ignore[arg-type]
    )


def _sample() -> list[AttentionItem]:
    return order_items(
        [
            _item(
                RUN_STALLED,
                BLOCKED,
                "run-a",
                "2026-07-06T05:00:00+00:00",
                cluster="hoffman2",
                evidence={"next_tick_due": "2026-07-06T06:00:00+00:00"},
            ),
            _item(
                RUN_ANOMALY,
                VERDICT,
                "run-b",
                "2026-07-06T09:00:00+00:00",
                action="classify-failed-tasks",
            ),
            _item(
                ALERT,
                INFORMATIONAL,
                "2026-07-06T11:30:00+00:00",
                "2026-07-06T11:30:00+00:00",
                action="driver stalled — re-arm?",
            ),
        ]
    )


def test_render_has_the_single_computed_at_header() -> None:
    out = render_queue(_sample(), computed_at=_NOW)
    first = out.splitlines()[0]
    assert first == f"attention queue · computed {_NOW} · re-run for current state"
    # Exactly one computed_at stamp in the whole digest.
    assert out.count("computed 2026-07-06T12:00:00") == 1


def test_render_ages_are_relative_to_computed_at() -> None:
    out = render_queue(_sample(), computed_at=_NOW)
    # 7h (stalled since 05:00), 3h (anomaly since 09:00), 30m (alert since 11:30).
    assert "7h · run-stalled" in out
    assert "3h · run-anomaly" in out
    assert "30m · alert" in out


def test_render_action_and_evidence_oneliners() -> None:
    out = render_queue(_sample(), computed_at=_NOW)
    # Stalled has no action → composed evidence one-liner from its own fields.
    assert "driver stalled; next tick was due 2026-07-06T06:00:00+00:00" in out
    # Anomaly's action DATA rides verbatim.
    assert "— classify-failed-tasks" in out
    # Cluster is surfaced when present.
    assert "run-a on hoffman2" in out


def test_render_is_byte_stable_under_input_shuffle() -> None:
    items = _sample()
    baseline = render_queue(order_items(items), computed_at=_NOW)
    shuffled = list(items)
    random.Random(3).shuffle(shuffled)
    assert render_queue(order_items(shuffled), computed_at=_NOW) == baseline


def test_render_sections_follow_class_order_override() -> None:
    items = order_items(_sample(), class_order=["informational"])
    out = render_queue(items, computed_at=_NOW)
    heads = [ln for ln in out.splitlines() if ln.startswith("## ")]
    assert heads[0] == "## informational"  # override reflected in the render


def test_render_empty_queue() -> None:
    out = render_queue([], computed_at=_NOW)
    assert out.splitlines()[0].startswith("attention queue · computed")
    assert "(nothing needs your attention)" in out
    assert "0 item(s)" in out


def test_render_undated_item_shows_age_placeholder() -> None:
    items = [_item(RUN_STALLED, BLOCKED, "r", None)]
    out = render_queue(items, computed_at=_NOW)
    assert "age? · run-stalled" in out


def test_render_surfaces_fanout_as_honest_count_only_when_nonzero() -> None:
    """The D2-revision fan-out rides the line as ``unblocks N`` (honest count),
    only when > 0, never as urgency prose."""
    with_edge = _item(RUN_ANOMALY, VERDICT, "hi", "2026-07-06T09:00:00+00:00", unblocks=3)
    no_edge = _item(RUN_STALLED, BLOCKED, "lo", "2026-07-06T05:00:00+00:00")
    out = render_queue(order_items([with_edge, no_edge]), computed_at=_NOW)
    assert "unblocks 3" in out
    # A fan-out-0 item never renders an "unblocks" token.
    stalled_line = next(ln for ln in out.splitlines() if "run-stalled" in ln)
    assert "unblocks" not in stalled_line
    # No urgency vocabulary is ever composed.
    assert "URGENT" not in out and "critical" not in out.lower()
