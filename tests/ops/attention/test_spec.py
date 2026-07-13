"""Tests for the attention-queue wire models (Wave B / T2, D7).

Spec validation: unknown class names in ``class_order`` are ACCEPTED, not refused
(the T12 semantics — order_items ignores unknowns at runtime, the spec never
gatekeeps them). The item model round-trips the D1 dict with the ``class`` alias.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hpc_agent._wire.queries.attention_queue import (
    AttentionItemModel,
    AttentionQueueResult,
    AttentionQueueSpec,
)
from hpc_agent.ops.attention_queue import (
    BLOCKED,
    RUN_STALLED,
    AttentionItem,
)


def test_spec_defaults() -> None:
    spec = AttentionQueueSpec()
    assert spec.fleet is False
    assert spec.class_order is None
    assert spec.now is None


def test_spec_accepts_unknown_class_names_not_refused() -> None:
    """Unknown class names ride through — the T12 'unknown ignored' rule lives in
    order_items, never in spec validation."""
    spec = AttentionQueueSpec(class_order=["verdict", "bogus", "blocked"])
    assert spec.class_order == ["verdict", "bogus", "blocked"]


def test_spec_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        AttentionQueueSpec(mark_seen=True)  # type: ignore[call-arg]


def test_item_model_round_trips_the_class_alias() -> None:
    item = AttentionItem(
        kind=RUN_STALLED,
        item_class=BLOCKED,
        experiment_dir="/exp",
        scope_kind="run",
        scope_id="run-a",
        block="submit-s3",
        since="2026-07-06T05:00:00+00:00",
        evidence={"next_tick_due": "2026-07-06T06:00:00+00:00"},
    )
    model = AttentionItemModel.model_validate(item.as_dict())
    assert model.kind == RUN_STALLED
    assert model.item_class == BLOCKED
    assert model.subject.scope_kind == "run"
    assert model.subject.scope_id == "run-a"
    assert model.subject.block == "submit-s3"
    # The wire dump uses the 'class' key (the D1 shape), not 'item_class'.
    dumped = model.model_dump(by_alias=True)
    assert dumped["class"] == BLOCKED
    assert "item_class" not in dumped


def test_item_model_carries_fanout_unblocks() -> None:
    """The D2-revision LEVERAGE key round-trips through as_dict → the wire model."""
    item = AttentionItem(
        kind=RUN_STALLED,
        item_class=BLOCKED,
        experiment_dir="/exp",
        scope_kind="run",
        scope_id="run-a",
        unblocks=4,
    )
    model = AttentionItemModel.model_validate(item.as_dict())
    assert model.unblocks == 4
    # Default is 0 (no encoded edge) — never a fabricated number.
    assert (
        AttentionItemModel.model_validate(
            AttentionItem(
                kind=RUN_STALLED,
                item_class=BLOCKED,
                experiment_dir="/exp",
                scope_kind="run",
                scope_id="run-b",
            ).as_dict()
        ).unblocks
        == 0
    )


def test_result_carries_render_and_counts() -> None:
    result = AttentionQueueResult.model_validate(
        {
            "computed_at": "2026-07-06T12:00:00+00:00",
            "items": [],
            "counts": {BLOCKED: 2},
            "skipped": [{"ref": "ns1", "reason": "torn"}],
            "render": "attention queue · computed ...",
        }
    )
    assert result.counts == {BLOCKED: 2}
    assert result.skipped[0].ref == "ns1"
    assert result.render.startswith("attention queue")
