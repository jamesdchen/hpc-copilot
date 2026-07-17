"""Behaviour-pinning coverage for :mod:`hpc_agent.infra.block_chain`.

The 2026-07-17 mutation triage-2 (``docs/plans/mutation-triage-2-2026-07-17.md``)
found the curated matrix ran DARK — block_chain produced zero mutation verdicts,
so nothing confirmed which surviving boundary/operator/default mutants the suite
kills. block_chain is the workflow-sequencing spine the block-drive driver reads:
ORDER, GATED_BLOCKS, the ``(verb, stage) → successor`` table, the run-13 wedge-fix
``chain_successor`` None-marker boundary, and the run-14 #4 spec composer. A silent
mutation here is a silent SEQUENCING bug — a gated block chained without a
greenlight, a wrong deadline that awaits a wedged child forever, a canary id
dropped from a composed spec.

``tests/ops/test_block_chain.py``, ``tests/contracts/test_spec_hint_completeness.py``,
and ``tests/_kernel/lifecycle/test_block_verb_deadlines.py`` pin the headline
behaviours; this file ADDS the boundary/operator/default pins those leave a mutant
free to survive: the ``>= 0`` budget floor + the bool/non-mapping guards in
``_spec_wall_clock_budget``, the exact per-class deadline constants, the ORDER
structure, the KeyError-vs-lenient contracts of ``workflow_of`` / ``successor_verb``,
the shaper's field-preservation + idempotence, and the composer's canary-carry /
flat-fallback / refuse branches.

Every assertion notes the mutation it kills inline.
"""

from __future__ import annotations

import pytest

from hpc_agent.infra import block_chain
from hpc_agent.infra.block_chain import (
    _HEAVY_VERB_DEADLINE_SEC,
    _QUICK_VERB_DEADLINE_SEC,
    _RECOVERY_ARM_BY_FIELD,
    _complete_spec_hint,
    _spec_wall_clock_budget,
    _wrap_run_id_under,
    chain_successor,
    compose_successor_spec,
    successor_verb,
)

_RUN_ID = "ml_run_abcd1234"
_CANARY_RUN_ID = "ml_run_abcd1234_canary"


# ── ORDER / membership identities ──────────────────────────────────────────────


def test_order_chains_are_the_exact_verb_sequences() -> None:
    # kills: reordering or dropping a verb within a family chain — block_index and
    # §4 field-change routing read the POSITION, so a swapped pair mis-routes a
    # rewind vs an advance.
    assert block_chain.ORDER["submit"] == ["submit-s1", "submit-s2", "submit-s3", "submit-s4"]
    assert block_chain.ORDER["status"] == ["status-snapshot", "status-watch"]
    assert block_chain.ORDER["aggregate"] == ["aggregate-check", "aggregate-run"]
    assert block_chain.ORDER["campaign"] == [
        "campaign-greenlight",
        "campaign-watch",
        "campaign-complete",
    ]
    # campaign-refill is its OWN single-member family (a side-spur, NOT in the
    # campaign chain — else it would shift the campaign block_index positions).
    assert block_chain.ORDER["campaign-refill"] == ["campaign-refill"]


def test_gated_blocks_is_exactly_the_four_greenlight_gated_verbs() -> None:
    # kills: adding/removing a GATED_BLOCKS member — this set is the SoT for
    # "park before entering". A dropped member lets the driver chain into a gated
    # block without the human greenlight its op gate requires.
    assert (
        frozenset({"submit-s2", "submit-s3", "submit-s4", "aggregate-run"})
        == block_chain.GATED_BLOCKS
    )
    for verb in block_chain.GATED_BLOCKS:
        assert block_chain.is_gated(verb) is True
    for ungated in ("submit-s1", "status-watch", "campaign-watch", "aggregate-check"):
        assert block_chain.is_gated(ungated) is False


def test_watch_verbs_is_exactly_the_three_spec_budgeted_verbs() -> None:
    # kills: adding/removing a WATCH_VERBS member — a watch verb gets its own
    # wall-clock budget + slack, not a class constant. submit-s3 is BOTH watch and
    # in DEADLINE_SECONDS, so dropping it flips it to the heavy constant.
    assert frozenset({"submit-s3", "status-watch", "campaign-watch"}) == block_chain.WATCH_VERBS


def test_recovery_arm_field_map_is_only_cluster() -> None:
    # kills: widening the delta→arm map — only a ``cluster`` delta selects an arm
    # (retarget-run); every other field stays a human branch.
    assert _RECOVERY_ARM_BY_FIELD == {"cluster": "retarget-run"}


# ── _spec_wall_clock_budget: the numeric guards + candidate precedence ──────────


def test_budget_zero_is_honored_not_treated_as_absent() -> None:
    # kills: ``value >= 0`` → ``value > 0`` — a legitimate zero budget must be
    # returned, not fall through to the default ceiling.
    assert _spec_wall_clock_budget({"monitor": {"wall_clock_budget_seconds": 0}}) == 0.0


def test_budget_negative_is_rejected() -> None:
    # kills: dropping the ``value >= 0`` lower bound — a negative budget is not a
    # real ceiling, so it falls through to None (→ the default).
    assert _spec_wall_clock_budget({"wall_clock_budget_seconds": -5}) is None


def test_budget_bool_is_rejected() -> None:
    # kills: dropping the ``not isinstance(value, bool)`` guard — True is an int
    # subclass, but a boolean is never a wall-clock budget.
    assert _spec_wall_clock_budget({"monitor": {"wall_clock_budget_seconds": True}}) is None


def test_budget_monitor_nesting_wins_over_top_level() -> None:
    # kills: swapping the ``(spec.get("monitor"), spec)`` candidate order — the
    # nested monitor budget takes precedence over a top-level one.
    spec = {"monitor": {"wall_clock_budget_seconds": 100}, "wall_clock_budget_seconds": 999}
    assert _spec_wall_clock_budget(spec) == 100.0


def test_budget_non_mapping_spec_is_none() -> None:
    # kills: dropping the ``isinstance(spec, Mapping)`` guard — a non-mapping spec
    # (or None) has no budget.
    assert _spec_wall_clock_budget([1, 2, 3]) is None  # type: ignore[arg-type]
    assert _spec_wall_clock_budget(None) is None


# ── verb_deadline_seconds: exact class constants + budget-ignore for non-watch ──


def test_quick_and_heavy_class_constants_are_exact() -> None:
    # kills: mutating the quick/heavy constants (600 / 3600) or swapping which
    # class a verb is assigned in DEADLINE_SECONDS. test_block_verb_deadlines only
    # asserts quick < heavy — this pins the actual seconds.
    assert _QUICK_VERB_DEADLINE_SEC == 600.0
    assert _HEAVY_VERB_DEADLINE_SEC == 3600.0
    assert block_chain.verb_deadline_seconds("submit-s1", {}) == 600.0
    assert block_chain.verb_deadline_seconds("status-snapshot", {}) == 600.0
    assert block_chain.verb_deadline_seconds("aggregate-check", {}) == 600.0
    assert block_chain.verb_deadline_seconds("campaign-greenlight", {}) == 600.0
    assert block_chain.verb_deadline_seconds("submit-s2", {}) == 3600.0
    assert block_chain.verb_deadline_seconds("submit-s4", {}) == 3600.0
    assert block_chain.verb_deadline_seconds("aggregate-run", {}) == 3600.0
    assert block_chain.verb_deadline_seconds("campaign-complete", {}) == 600.0


def test_non_watch_known_verb_ignores_a_spec_budget() -> None:
    # kills: the ``verb in WATCH_VERBS or verb not in DEADLINE_SECONDS`` disjunction
    # collapsing so a KNOWN non-watch verb reads the spec budget. submit-s2 has a
    # budget on its spec, but its deadline is the fixed heavy constant.
    spec = {"monitor": {"wall_clock_budget_seconds": 100}}
    assert block_chain.verb_deadline_seconds("submit-s2", spec) == 3600.0


# ── chain_successor / workflow_of / block_index: the static-order neighbour ─────


def test_chain_successor_covers_every_interior_neighbour() -> None:
    # kills: the ``order[idx + 1]`` offset or the ``idx + 1 < len`` boundary. Each
    # interior verb chains to its immediate ORDER neighbour; each family terminal
    # (and campaign-refill) chains to None.
    assert chain_successor("submit-s2") == "submit-s3"
    assert chain_successor("status-snapshot") == "status-watch"
    assert chain_successor("campaign-greenlight") == "campaign-watch"
    assert chain_successor("campaign-watch") == "campaign-complete"
    for terminal in ("submit-s4", "status-watch", "aggregate-run", "campaign-complete"):
        assert chain_successor(terminal) is None
    assert chain_successor("campaign-refill") is None
    assert chain_successor("totally-unknown-verb") is None  # unknown family → None


def test_workflow_of_raises_on_unknown_verb() -> None:
    # kills: workflow_of degrading to a lenient default — an unknown verb is a
    # programming error (a typo / unregistered block), so it RAISES, unlike the
    # lenient successor_verb.
    with pytest.raises(KeyError):
        block_chain.workflow_of("no-such-verb")


def test_block_index_positions_and_unknown_raises() -> None:
    # kills: mutating block_index's ``.index`` call and the unknown-verb contract.
    assert block_chain.block_index("submit-s2") == 1
    assert block_chain.block_index("submit-s3") == 2
    assert block_chain.block_index("status-watch") == 1
    assert block_chain.block_index("campaign-watch") == 1
    with pytest.raises(KeyError):
        block_chain.block_index("no-such-verb")


# ── successor_verb: the status / aggregate / campaign family values ─────────────


def test_successor_table_status_and_aggregate_families() -> None:
    # kills: a wrong successor / a decision-park entry flipped to a chain in the
    # status + aggregate families (test_block_chain spells out only submit).
    assert successor_verb("status-snapshot", "snapshot_clean") == "status-watch"
    assert successor_verb("status-watch", "watch_terminal") == "submit-s4"
    assert successor_verb("status-watch", "watch_timeout") == "status-watch"  # self-loop
    assert successor_verb("aggregate-check", "ready") == "aggregate-run"
    # Human-branch terminators → None.
    assert successor_verb("status-snapshot", "snapshot_anomaly") is None
    assert successor_verb("aggregate-check", "not_ready") is None
    assert successor_verb("aggregate-check", "integrity_review") is None
    assert successor_verb("aggregate-run", "harvested") is None


def test_successor_table_campaign_family() -> None:
    # kills: a flipped campaign edge — both greenlit + already_greenlit chain to
    # watch (idempotent re-read), watching_complete → complete, the refill spur,
    # and the async self-chain / anomaly branches stay None.
    assert successor_verb("campaign-greenlight", "greenlit") == "campaign-watch"
    assert successor_verb("campaign-greenlight", "already_greenlit") == "campaign-watch"
    assert successor_verb("campaign-watch", "watching_complete") == "campaign-complete"
    assert successor_verb("campaign-watch", "watching_refill") == "campaign-refill"
    assert successor_verb("campaign-greenlight", "needs_greenlight") is None
    assert successor_verb("campaign-watch", "watching_healthy") is None
    assert successor_verb("campaign-watch", "watching_anomaly") is None
    assert successor_verb("campaign-complete", "complete") is None


# ── next_block_hint / the status-watch shaper: wrap + preserve + idempotence ────


def test_next_block_hint_wraps_flat_run_id_under_monitor_for_status_watch() -> None:
    # kills: the status-watch shaper not firing — a flat ``run_id`` terminator
    # (submit-s3 watching_timeout) must be reshaped to the nested ``monitor`` shape
    # StatusWatchSpec requires, else the driver hands it a spec its validator bounces.
    hint = block_chain.next_block_hint("submit-s3", "watching_timeout", why="x", run_id=_RUN_ID)
    assert hint == {
        "verb": "status-watch",
        "why": "x",
        "spec_hint": {"monitor": {"run_id": _RUN_ID}},
    }


def test_shaper_preserves_non_run_id_fields_alongside_the_nested_key() -> None:
    # kills: the reshape dropping sibling fields — non-run_id hint fields survive
    # next to the nested ``monitor`` block.
    shaped = _complete_spec_hint(
        "status-watch", {"run_id": _RUN_ID, "canary_run_id": _CANARY_RUN_ID}
    )
    assert shaped == {"monitor": {"run_id": _RUN_ID}, "canary_run_id": _CANARY_RUN_ID}


def test_shaper_is_idempotent_and_no_op_without_run_id() -> None:
    # kills: the ``run_id is None or nested_key in spec_hint`` no-op guard — an
    # already-nested hint (or one carrying no flat run_id) is left untouched, never
    # double-wrapped.
    already = {"monitor": {"run_id": _RUN_ID}}
    assert _complete_spec_hint("status-watch", dict(already)) == already
    assert _wrap_run_id_under("monitor")({"campaign_id": "c1"}) == {"campaign_id": "c1"}


def test_complete_spec_hint_passes_through_a_non_shaped_successor() -> None:
    # kills: a shaper applied to the wrong successor — submit-s3 has no shaper, so
    # its hint passes through verbatim (no accidental monitor-wrap).
    hint = {"run_id": _RUN_ID, "canary_run_id": _CANARY_RUN_ID}
    assert _complete_spec_hint("submit-s3", dict(hint)) == hint


# ── compose_successor_spec: canary-carry / flat-fallback / refuse branches ──────


def test_compose_s3_carries_the_canary_ids_from_the_hint() -> None:
    # kills: the ``if value is not None`` canary-carry loop being dropped — the
    # canary ids are OPTIONAL on SubmitS3Spec, so a validator would still pass if
    # they were dropped; only this content assertion catches the loss.
    composed = compose_successor_spec(
        "submit-s3",
        spec_hint={"run_id": _RUN_ID, "canary_run_id": _CANARY_RUN_ID, "canary_job_ids": ["12344"]},
        predecessor_spec={"submit": {"x": 1}},
    )
    assert composed["submit"] == {"x": 1}  # reused verbatim, never re-authored
    assert composed["monitor"] == {"run_id": _RUN_ID}  # derived from run_id
    assert composed["canary_run_id"] == _CANARY_RUN_ID
    assert composed["canary_job_ids"] == ["12344"]


def test_compose_s3_omits_absent_canary_ids() -> None:
    # kills: a mutation that always injects the canary keys (with None) — an absent
    # canary id must NOT appear in the composed spec.
    composed = compose_successor_spec(
        "submit-s3", spec_hint={"run_id": _RUN_ID}, predecessor_spec={"submit": {"x": 1}}
    )
    assert "canary_run_id" not in composed
    assert "canary_job_ids" not in composed


def test_compose_aggregate_run_flat_run_id_fallback() -> None:
    # kills: dropping the flat ``run_id`` fallback — a caller that passed a bare
    # run_id (not the nested ``aggregate`` hint) still composes a valid spec.
    assert compose_successor_spec("aggregate-run", spec_hint={"run_id": _RUN_ID}) == {
        "aggregate": {"run_id": _RUN_ID}
    }


def test_compose_aggregate_run_refuses_when_no_run_id() -> None:
    # kills: dropping the final ``raise`` — an empty hint cannot name the run, so
    # the composer REFUSES (never fabricates), naming the missing field.
    with pytest.raises(block_chain.SuccessorSpecIncomplete) as ei:
        compose_successor_spec("aggregate-run", spec_hint={})
    assert ei.value.successor == "aggregate-run"
    assert ei.value.missing == "aggregate.run_id"


def test_compose_s2_refuses_a_non_dict_resolve_brief() -> None:
    # kills: dropping the ``isinstance(resolve, dict)`` guard — a malformed resolve
    # leg must refuse via SuccessorSpecIncomplete, not AttributeError on ``.get``.
    with pytest.raises(block_chain.SuccessorSpecIncomplete) as ei:
        compose_successor_spec("submit-s2", spec_hint={}, result_brief={"resolve": "not-a-dict"})
    assert ei.value.missing == "submit"


def test_compose_ungated_successor_returns_the_hint_verbatim() -> None:
    # kills: the ``composer is None`` branch — an ungated successor's hint is
    # already the complete shaped spec, returned as a copy, never re-composed.
    hint = {"monitor": {"run_id": _RUN_ID}}
    out = compose_successor_spec("status-watch", spec_hint=hint)
    assert out == hint
    assert out is not hint  # a copy, so a later mutation of the hint cannot leak in


# ── successor_spec_sha: byte-stable identity, drift-detecting ───────────────────


def test_successor_spec_sha_is_stable_and_moves_on_edit() -> None:
    # kills: a non-deterministic serialization (dropping ``sort_keys``) or a sha
    # that ignores content. Same content (any key order) → same sha; any edit → a
    # different sha (the R3 drift-pin foundation).
    a = {"submit": {"run_id": _RUN_ID}, "monitor": {"run_id": _RUN_ID}}
    b = {"monitor": {"run_id": _RUN_ID}, "submit": {"run_id": _RUN_ID}}  # reordered keys
    assert block_chain.successor_spec_sha(a) == block_chain.successor_spec_sha(b)
    tampered = {**a, "monitor": {"run_id": "SOMEONE_ELSE"}}
    assert block_chain.successor_spec_sha(tampered) != block_chain.successor_spec_sha(a)
