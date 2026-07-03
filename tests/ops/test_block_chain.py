"""Tests for the re-homed next_block chaining table (block-drive.md §6/§8).

``infra/block_chain.py`` is the single source of truth for the deterministic block
successor. These assert (a) the table's own invariants (membership / ordering /
lookup policy) and (b) that the table AGREES with what the submit block module
actually emits — the guard against the table silently drifting from the inline
terminators it was lifted out of.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest import mock

import hpc_agent.ops.submit_blocks as submit_blocks
from hpc_agent.infra import block_chain
from hpc_agent.infra.block_chain import (
    ORDER,
    SUCCESSORS,
    WORKFLOW_OF,
    block_index,
    next_block_hint,
    successor_verb,
    workflow_of,
)

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "ml_run_abcd1234"


# ── table invariants ──────────────────────────────────────────────────────────


def test_workflow_of_derived_from_order() -> None:
    """Every verb in ORDER is in WORKFLOW_OF under its family, and vice versa."""
    for workflow, verbs in ORDER.items():
        for verb in verbs:
            assert WORKFLOW_OF[verb] == workflow
            assert workflow_of(verb) == workflow
    # No stray verbs — WORKFLOW_OF is exactly the union of the ORDER chains.
    assert set(WORKFLOW_OF) == {v for verbs in ORDER.values() for v in verbs}


def test_block_index_matches_order_position() -> None:
    assert block_index("submit-s1") == 0
    assert block_index("submit-s4") == 3
    assert block_index("status-snapshot") == 0
    assert block_index("aggregate-run") == 1
    assert block_index("campaign-complete") == 2


def test_every_successor_verb_is_a_known_block() -> None:
    """A non-None successor must itself be a registered block verb (no dangling)."""
    for (current, _stage), succ in SUCCESSORS.items():
        assert current in WORKFLOW_OF, current
        if succ is not None:
            assert succ in WORKFLOW_OF, succ


def test_successor_verb_unknown_pair_is_none() -> None:
    """Lookup policy: an unknown (verb, stage) pair → None (a human branch)."""
    assert successor_verb("submit-s1", "no-such-stage") is None
    assert successor_verb("no-such-verb", "resolved") is None


def test_successor_table_submit_family_values() -> None:
    """The submit family's deterministic chain, spelled out."""
    assert successor_verb("submit-s1", "resolved") == "submit-s2"
    assert successor_verb("submit-s2", "canary_verified") == "submit-s3"
    assert successor_verb("submit-s3", "watching_terminal") == "submit-s4"
    assert successor_verb("submit-s3", "watching_timeout") == "status-watch"
    # Human branches / terminals → None.
    assert successor_verb("submit-s1", "needs_resolution") is None
    assert successor_verb("submit-s1", "prior_run_found") is None
    assert successor_verb("submit-s2", "canary_failed") is None
    assert successor_verb("submit-s3", "watching_anomaly") is None
    assert successor_verb("submit-s4", "harvested") is None
    assert successor_verb("submit-s4", "harvest_partial") is None


# ── next_block_hint shape ──────────────────────────────────────────────────────


def test_next_block_hint_builds_shape_from_table() -> None:
    hint = next_block_hint("submit-s2", "canary_verified", why="go", run_id=_RUN_ID)
    assert hint == {
        "verb": "submit-s3",
        "why": "go",
        "spec_hint": {"run_id": _RUN_ID},
    }


def test_next_block_hint_none_at_terminator() -> None:
    """A stage with no deterministic successor yields None (not a dict)."""
    assert next_block_hint("submit-s2", "canary_failed", why="x", run_id=_RUN_ID) is None


# ── table AGREES with the module (the anti-drift guard) ───────────────────────


def _submit_flow_spec() -> Any:
    from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec, SubmitResources

    return SubmitFlowSpec(
        profile="ml",
        cluster="hoffman2",
        ssh_target="user@hoffman2.idre.ucla.edu",
        remote_path="/u/scratch/exp",
        job_name="ml",
        run_id=_RUN_ID,
        total_tasks=10,
        backend="slurm",
        script=".hpc/templates/cpu_array.sh",
        job_env={"K": "v"},
        canary=True,
        resources=SubmitResources(walltime_sec=3600, cpus=4),
    )


def _assert_emitted_agrees(result: Any) -> None:
    """A block's emitted next_block['verb'] must equal the table's successor."""
    # submit blocks report block="s1".."s4"; map to the "submit-sN" verb.
    verb = {"s1": "submit-s1", "s2": "submit-s2", "s3": "submit-s3", "s4": "submit-s4"}[
        result.block
    ]
    expected = successor_verb(verb, result.stage_reached)
    if result.next_block is None:
        # None is allowed either as a table terminator OR a runtime-gated branch;
        # when the table HAS a successor and the block emitted None, that is only
        # legitimate for the documented runtime-gated cases (none in submit).
        assert expected is None, (verb, result.stage_reached, expected)
    else:
        assert result.next_block["verb"] == expected


def test_submit_s1_clean_resolved_agrees_with_table(tmp_path: Path) -> None:
    from hpc_agent._wire.queries.walk_submit_ambiguities import WalkSubmitAmbiguitiesInput
    from hpc_agent._wire.workflows.submit_blocks import SubmitS1Spec

    walk = WalkSubmitAmbiguitiesInput.model_validate(
        {
            "cluster": "hoffman2",
            "goal": "g",
            "tasks_py_present": True,
            "entry_point_resolved": True,
            "data_axis_resolved": True,
            "homogeneous_axes_resolved": True,
        }
    )
    result = submit_blocks.submit_s1(tmp_path, spec=SubmitS1Spec(walk=walk, run_preflight=False))

    assert result.stage_reached == "resolved"
    assert result.next_block is not None
    assert result.next_block["verb"] == "submit-s2"
    _assert_emitted_agrees(result)


def test_submit_s2_canary_verified_agrees_with_table(tmp_path: Path) -> None:
    from hpc_agent._wire.workflows.submit_and_verify import (
        SubmitAndVerifyResult,
        SubmitAndVerifySpec,
    )
    from hpc_agent._wire.workflows.submit_blocks import SubmitS2Spec
    from hpc_agent.state.decision_journal import append_decision

    # The S2 gate needs a journaled greenlight naming submit-s2.
    append_decision(
        tmp_path,
        scope_kind="run",
        scope_id=_RUN_ID,
        block="test-greenlight",
        response="y",
        resolved={"next_block": "submit-s2"},
    )

    sv_spec = SubmitAndVerifySpec(
        submit=_submit_flow_spec(), poll_interval_sec=1, wait_budget_sec=5
    )
    sv_result = SubmitAndVerifyResult(
        run_id=_RUN_ID,
        job_ids=["999"],
        total_tasks=10,
        deduped=False,
        canary_run_id=f"{_RUN_ID}_canary",
        canary_job_ids=["12344"],
        verified=True,
        failure_kind=None,
        verify_result=None,
    )

    with mock.patch.object(submit_blocks, "submit_and_verify", return_value=sv_result):
        result = submit_blocks.submit_s2(tmp_path, spec=SubmitS2Spec(submit=sv_spec, detach=False))

    assert result.stage_reached == "canary_verified"
    assert result.next_block is not None
    assert result.next_block["verb"] == "submit-s3"
    _assert_emitted_agrees(result)


def test_submit_s2_canary_failed_emits_no_next_block(tmp_path: Path) -> None:
    """A failed canary is a human-branch terminator — table and module both None."""
    from hpc_agent._wire.workflows.submit_and_verify import (
        SubmitAndVerifyResult,
        SubmitAndVerifySpec,
    )
    from hpc_agent._wire.workflows.submit_blocks import SubmitS2Spec
    from hpc_agent.state.decision_journal import append_decision

    append_decision(
        tmp_path,
        scope_kind="run",
        scope_id=_RUN_ID,
        block="test-greenlight",
        response="y",
        resolved={"next_block": "submit-s2"},
    )
    sv_spec = SubmitAndVerifySpec(
        submit=_submit_flow_spec(), poll_interval_sec=1, wait_budget_sec=5
    )
    sv_result = SubmitAndVerifyResult(
        run_id=_RUN_ID,
        job_ids=[],
        total_tasks=10,
        deduped=False,
        canary_run_id=f"{_RUN_ID}_canary",
        canary_job_ids=["12344"],
        verified=False,
        failure_kind="nonzero_exit",
        verify_result=None,
    )
    with mock.patch.object(submit_blocks, "submit_and_verify", return_value=sv_result):
        result = submit_blocks.submit_s2(tmp_path, spec=SubmitS2Spec(submit=sv_spec, detach=False))

    assert result.stage_reached == "canary_failed"
    assert result.next_block is None
    assert successor_verb("submit-s2", "canary_failed") is None


def test_module_helper_delegates_to_block_chain() -> None:
    """The submit module's _next_block delegates to block_chain.next_block_hint."""
    assert submit_blocks._next_block("submit-s1", "resolved", "why") == {
        "verb": "submit-s2",
        "why": "why",
        "spec_hint": {},
    }
    assert submit_blocks._next_block("submit-s1", "prior_run_found", "why") is None
    # Sanity: the module imports the shared builder, not a private copy.
    assert submit_blocks.next_block_hint is block_chain.next_block_hint
