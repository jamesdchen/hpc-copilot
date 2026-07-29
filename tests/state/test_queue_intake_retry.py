"""The intake ledger's declared-retry vocabulary — §7 failure classes.

The retryable(n) leg (run-queue plan §7 "Failure classes on parked items",
RESOLVED as proposed 2026-07-29) stands on four ledger facts, each with one
reader here and each pinned with the case that makes its guard fire:

1. **The budget is DECLARED or it does not exist.** ``item_retryable`` reads
   an explicit small positive int and nothing else — every junk shape a
   hand-edited or foreign ledger line could carry reads as ``None``
   (needs_human), because a budget the enqueuer did not declare must not be
   conjured by a tolerant reader.
2. **The retry identity is DERIVED and charset-legal.** ``retry_request_id``
   composes ``<root>.retry<k>`` inside the ``QueueItemId`` charset (the
   plan's sketch spelled ``#retry``, which the wire would refuse), and the
   derived id doubles as the append's dedup ``request_id`` — the fact that
   makes racing producers write ONE line, asserted against the real
   ``append_intake_item`` below rather than assumed.
3. **Counting is durable — from the ledger, never memory.** ``retries_used``
   is the max ``retry_attempt`` over the chain's folded items; the chain
   grouping (``retry_chains``) and the tip selection (``retry_tip``) are the
   one shared derivation both the dispatch producer and ``queue-status``
   consume.
4. **A retry enqueue is an ordinary arrival.** The fold treats a kernel-minted
   retry item exactly like any enqueue (state ``queued``, own item_id), so
   nothing downstream needed a new ledger state — pinned by folding a real
   root + retry pair off disk.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent.state.queue_intake import (
    RETRYABLE_CAP,
    append_intake_item,
    item_retry_attempt,
    item_retry_root,
    item_retryable,
    read_intake_items,
    retries_used,
    retry_chains,
    retry_request_id,
    retry_tip,
)

if TYPE_CHECKING:
    from pathlib import Path

# ── 1. the budget is declared or it does not exist ───────────────────────────


def test_item_retryable_reads_only_a_declared_small_positive_int() -> None:
    assert item_retryable({"retryable": 1}) == 1
    assert item_retryable({"retryable": 3}) == 3
    assert item_retryable({"retryable": RETRYABLE_CAP}) == RETRYABLE_CAP


@pytest.mark.parametrize(
    "junk",
    [
        None,  # the declared default: needs_human
        0,  # not a budget
        -2,  # not a budget
        RETRYABLE_CAP + 1,  # past the cap the wire refuses
        True,  # bool is not an int budget (isinstance(True, int) is the trap)
        3.0,  # a float is a foreign writer's shape
        "3",  # a quoted number is a hand edit
        {"n": 3},  # structure is not a declaration
    ],
)
def test_item_retryable_refuses_junk_as_needs_human(junk: Any) -> None:
    """Absent and ill-shaped read identically: no declaration, no budget."""
    item = {} if junk is None else {"retryable": junk}
    assert item_retryable(item) is None


def test_wire_le_bound_mirrors_retryable_cap() -> None:
    """The MIRROR pin: ``QueueRunSpec.retryable``'s ``le=`` bound IS
    ``RETRYABLE_CAP`` — the wire refuses junk at enqueue and the ledger reader
    refuses the same junk tolerantly, and the two ceilings drifting apart
    would let a wire-legal declaration read back as needs_human (or worse,
    the reverse)."""
    from hpc_agent._wire.actions.queue_run import QueueRunSpec

    metadata = QueueRunSpec.model_fields["retryable"].metadata
    le_bounds = [meta.le for meta in metadata if hasattr(meta, "le")]
    assert le_bounds == [RETRYABLE_CAP]
    ge_bounds = [meta.ge for meta in metadata if hasattr(meta, "ge")]
    assert ge_bounds == [1], "a budget of 0 is not a budget; the floor is declared too"


# ── 2. the derived retry identity ────────────────────────────────────────────


def test_retry_request_id_is_derived_and_charset_legal() -> None:
    """``<root>.retry<k>`` — inside the QueueItemId charset (never ``#``)."""
    import re

    rid = retry_request_id("item-1", 2)
    assert rid == "item-1.retry2"
    assert re.fullmatch(r"[A-Za-z0-9._\-]+", rid), "derived id left the wire charset"
    assert rid != "item-1", "a token equal to the root would dedup against its enqueue"


def test_retry_request_id_refuses_a_zeroth_attempt_and_a_blank_root() -> None:
    """Attempt 0 IS the root item; deriving an id for it would fork its identity."""
    with pytest.raises(ValueError):
        retry_request_id("item-1", 0)
    with pytest.raises(ValueError):
        retry_request_id("  ", 1)


def test_the_derived_id_is_the_dedup_key_so_racers_write_one_line(tmp_path: Path) -> None:
    """The race-safety fact itself, against the real appender: two producers
    that both decide 'attempt 1 is owed' derive the same request_id, and the
    second append writes NOTHING (the original record comes back)."""
    exp = tmp_path
    rid = retry_request_id("item-1", 1)
    first = append_intake_item(exp, record={"retry_attempt": 1}, request_id=rid)
    second = append_intake_item(exp, record={"retry_attempt": 1}, request_id=rid)
    assert first is None  # written
    assert second is not None and second["item_id"] == rid  # deduped, echoed
    assert len(read_intake_items(exp)) == 1


# ── 3. chains, tips, and the durable count ───────────────────────────────────


def _chain(*attempts: int) -> list[dict[str, Any]]:
    root = {"item_id": "item-1", "state": "placed"}
    out: list[dict[str, Any]] = [root]
    out += [
        {
            "item_id": f"item-1.retry{k}",
            "state": "placed",
            "retry_root": "item-1",
            "retry_attempt": k,
        }
        for k in attempts
    ]
    return out


def test_retry_root_and_attempt_default_to_self_and_zero() -> None:
    """A non-retry item is its own chain's root at attempt 0 — today's items
    read unchanged, which is what keeps the leg's default byte-identical."""
    plain = {"item_id": "item-9", "state": "queued"}
    assert item_retry_root(plain) == "item-9"
    assert item_retry_attempt(plain) == 0
    assert item_retry_attempt({"item_id": "x", "retry_attempt": "two"}) == 0
    assert item_retry_attempt({"item_id": "x", "retry_attempt": True}) == 0
    assert item_retry_attempt({"item_id": "x", "retry_attempt": -1}) == 0


def test_retry_chains_groups_by_root_and_keeps_singletons() -> None:
    items = [*_chain(1, 2), {"item_id": "other", "state": "queued"}]
    chains = retry_chains(items)
    assert set(chains) == {"item-1", "other"}
    assert [i["item_id"] for i in chains["item-1"]] == [
        "item-1",
        "item-1.retry1",
        "item-1.retry2",
    ]


def test_retries_used_is_the_max_attempt_never_a_count() -> None:
    """Max, not len: a chain missing its middle record (a mangled line) can
    only UNDER-report what was lost — it can never re-earn a spent attempt."""
    full = _chain(1, 2, 3)
    torn = [item for item in full if item["item_id"] != "item-1.retry2"]
    assert retries_used(full) == 3
    assert retries_used(torn) == 3
    assert retries_used(_chain()) == 0
    assert retries_used([]) == 0


def test_retry_tip_is_the_latest_attempt() -> None:
    chain = _chain(1, 2)
    tip = retry_tip(chain)
    assert tip is not None and tip["item_id"] == "item-1.retry2"
    only_root = _chain()
    assert retry_tip(only_root) is only_root[0]
    assert retry_tip([]) is None


# ── 4. a retry enqueue is an ordinary arrival on the real ledger ─────────────


def test_a_root_and_its_retry_fold_as_two_ordinary_items(tmp_path: Path) -> None:
    """No new ledger state: the retry arrives 'queued' with its chain facts as
    arrival facts, and the shared derivations read the chain off the fold."""
    exp = tmp_path
    append_intake_item(exp, record={"spec": {"x": 1}, "retryable": 2}, request_id="item-1")
    append_intake_item(
        exp,
        record={
            "spec": {"x": 1},
            "retryable": 2,
            "retry_root": "item-1",
            "retry_attempt": 1,
        },
        request_id=retry_request_id("item-1", 1),
    )
    items = read_intake_items(exp)
    assert [item["state"] for item in items] == ["queued", "queued"]
    chains = retry_chains(items)
    assert set(chains) == {"item-1"}
    assert retries_used(chains["item-1"]) == 1
    tip = retry_tip(chains["item-1"])
    assert tip is not None and tip["item_id"] == "item-1.retry1"
    assert item_retryable(tip) == 2
