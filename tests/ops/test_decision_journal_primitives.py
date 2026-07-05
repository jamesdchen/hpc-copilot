"""Direct-atom tests for the ``append-decision`` / ``read-decisions`` primitives.

Constructs the wire spec, calls the primitive, asserts on the result —
bypassing the generated JSON (per the primitive recipe's atom-test
minimum). Covers the append→read round-trip through the primitive layer,
append-only discipline, both scopes, and the greenlight/nudge responses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.decision_journal import AppendDecisionInput, AppendDecisionResult
from hpc_agent._wire.queries.decision_journal import ReadDecisionsInput
from hpc_agent.ops.decision.journal import append_decision, read_decisions

if TYPE_CHECKING:
    from pathlib import Path


def _append(tmp_path: Path, **overrides: object) -> AppendDecisionResult:
    base: dict[str, object] = {
        "scope_kind": "run",
        "scope_id": "run-1",
        "block": "submit.S1",
        "response": "y",
    }
    base.update(overrides)
    return append_decision(experiment_dir=tmp_path, spec=AppendDecisionInput.model_validate(base))


def test_append_returns_record_path_and_count(tmp_path: Path) -> None:
    out = _append(
        tmp_path,
        evidence_digest={"canary": "green", "core_hours": 128},
        proposal=["interpret as converged", "run one more wave"],
        response="no — one more wave",
        resolved={},
        provenance={"decided_by": "human", "surface": "slash"},
    )
    assert out.count == 1
    assert out.path.endswith("run-1.decisions.jsonl")
    assert out.record.response == "no — one more wave"
    assert out.record.scope_kind == "run"
    assert out.record.block == "submit.S1"
    assert out.record.ts  # auto-stamped
    assert out.record.schema_version == 1
    assert out.record.provenance == {"decided_by": "human", "surface": "slash"}


def test_append_read_round_trip_preserves_order(tmp_path: Path) -> None:
    _append(tmp_path, block="submit.S1", response="no — halve the grid")
    _append(tmp_path, block="submit.S1", response="y")
    _append(tmp_path, block="harvest", response="y")

    result = read_decisions(
        experiment_dir=tmp_path,
        spec=ReadDecisionsInput.model_validate({"scope_kind": "run", "scope_id": "run-1"}),
    )
    assert result.count == 3
    assert [(r.block, r.response) for r in result.records] == [
        ("submit.S1", "no — halve the grid"),
        ("submit.S1", "y"),
        ("harvest", "y"),
    ]


def test_append_only_second_never_clobbers_first(tmp_path: Path) -> None:
    first = _append(tmp_path, block="submit.S1", response="y")
    second = _append(tmp_path, block="anomaly", response="stop")
    assert first.count == 1
    assert second.count == 2
    result = read_decisions(
        experiment_dir=tmp_path,
        spec=ReadDecisionsInput.model_validate({"scope_kind": "run", "scope_id": "run-1"}),
    )
    assert result.records[0].block == "submit.S1"
    assert result.records[1].block == "anomaly"


def test_campaign_scope(tmp_path: Path) -> None:
    _append(
        tmp_path,
        scope_kind="campaign",
        scope_id="camp-1",
        block="campaign.spec",
        response="y",
        resolved={"strategy": "optuna", "budget": 500},
    )
    result = read_decisions(
        experiment_dir=tmp_path,
        spec=ReadDecisionsInput.model_validate({"scope_kind": "campaign", "scope_id": "camp-1"}),
    )
    assert result.count == 1
    assert result.path.endswith("camp-1/decisions.jsonl") or result.path.endswith(
        "camp-1\\decisions.jsonl"
    )
    assert result.records[0].resolved == {"strategy": "optuna", "budget": 500}


def test_read_empty_scope(tmp_path: Path) -> None:
    result = read_decisions(
        experiment_dir=tmp_path,
        spec=ReadDecisionsInput.model_validate({"scope_kind": "run", "scope_id": "nope"}),
    )
    assert result.count == 0
    assert result.records == []


# ─── next_block auto-default from the parked pending decision (proving run #2) ──


def _seed_pending(tmp_path: Path, run_id: str, *, next_verb: str) -> None:
    """Create a run record parked on a decision whose successor is *next_verb*."""
    from hpc_agent.state.journal import mark_pending_decision, upsert_run
    from hpc_agent.state.run_record import RunRecord

    upsert_run(
        tmp_path,
        RunRecord(
            run_id=run_id,
            profile="p",
            cluster="hoffman2",
            ssh_target="u@h",
            remote_path="/remote",
            job_name="j",
            job_ids=["100"],
            total_tasks=4,
            submitted_at="2026-07-03T00:00:00+00:00",
            experiment_dir=str(tmp_path),
            status="in_flight",
        ),
    )
    mark_pending_decision(
        run_id,
        block="submit-s1",
        workflow="submit",
        brief={"proposal": "greenlight the resolved plan?"},
        resume_cursor={"workflow": "submit", "run_id": run_id, "next_verb": next_verb},
        awaiting_since="2026-07-03T00:30:00+00:00",
        experiment_dir=tmp_path,
    )


def test_greenlight_defaults_next_block_from_pending_decision(tmp_path: Path) -> None:
    _seed_pending(tmp_path, "run-1", next_verb="submit-s2")
    out = _append(tmp_path, response="y", resolved={"cluster": "hoffman2"})
    assert out.record.resolved["next_block"] == "submit-s2"
    assert out.record.resolved["cluster"] == "hoffman2"  # existing fields preserved


def test_explicit_next_block_is_not_overridden(tmp_path: Path) -> None:
    _seed_pending(tmp_path, "run-1", next_verb="submit-s2")
    out = _append(tmp_path, response="y", resolved={"next_block": "submit-s3"})
    assert out.record.resolved["next_block"] == "submit-s3"


def test_nudge_response_is_not_defaulted(tmp_path: Path) -> None:
    _seed_pending(tmp_path, "run-1", next_verb="submit-s2")
    out = _append(tmp_path, response="no — halve the grid", resolved={"cluster": "hoffman2"})
    assert "next_block" not in out.record.resolved


def test_no_pending_decision_leaves_resolved_untouched(tmp_path: Path) -> None:
    out = _append(tmp_path, response="y", resolved={"cluster": "hoffman2"})
    assert "next_block" not in out.record.resolved


def test_append_rejects_bad_scope(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid):
        append_decision(
            experiment_dir=tmp_path,
            spec=AppendDecisionInput.model_validate(
                {"scope_kind": "run", "scope_id": "run-1", "block": "b", "response": "y"}
            ).model_copy(update={"scope_id": "../escape"}),
        )


def test_greenlight_defaults_next_block_from_chain_table_without_pending(tmp_path: Path) -> None:
    """MCP-direct mode (proving-run-3 re-fire): no block-drive park, no
    RunRecord at S1→S2 — the successor derives from the static chain table off
    the record's own ``block`` field."""
    out = _append(tmp_path, block="submit-s1", response="y", resolved={"cluster": "hoffman2"})
    assert out.record.resolved["next_block"] == "submit-s2"


def test_chain_fallback_matches_short_block_names(tmp_path: Path) -> None:
    # Records journal the short form ("s1") — suffix match resolves it.
    out = _append(tmp_path, block="s1", response="y", resolved={})
    assert out.record.resolved["next_block"] == "submit-s2"
    out = _append(tmp_path, block="s2", response="y", resolved={})
    assert out.record.resolved["next_block"] == "submit-s3"


def test_chain_fallback_never_guesses(tmp_path: Path) -> None:
    # Chain-final block: nothing to advance to.
    out = _append(tmp_path, block="submit-s4", response="y", resolved={})
    assert "next_block" not in out.record.resolved
    # Unknown block: no derivation.
    out = _append(tmp_path, block="submit.S1", response="y", resolved={})
    assert "next_block" not in out.record.resolved or out.record.resolved.get("next_block")


def test_pending_decision_wins_over_chain_table(tmp_path: Path) -> None:
    """A parked driver's next_verb is more specific than the static chain
    (e.g. a rerun of the SAME block after a nudge) — it takes precedence."""
    _seed_pending(tmp_path, "run-1", next_verb="submit-s1")  # rerun-same-block park
    out = _append(tmp_path, block="submit-s1", response="y", resolved={})
    assert out.record.resolved["next_block"] == "submit-s1"


# ─── provenance gate (conduct rule 9, proving-run-2-hardening §6) ──────────────


def _persist_brief(tmp_path: Path, run_id: str, block: str, brief: dict[str, object]) -> None:
    """Persist a block brief so the gate has something to diff against."""
    from hpc_agent.state.decision_briefs import append_brief

    append_brief(tmp_path, run_id=run_id, block=block, brief=brief)


def test_gate_passes_with_no_briefs_file(tmp_path: Path) -> None:
    """Fail-open on ABSENCE: no persisted brief → the gate never fires (old runs,
    campaign scope, tests that never persist a brief)."""
    out = _append(tmp_path, block="s1", response="y", resolved={"result_dir_template": "x"})
    assert out.record.resolved["result_dir_template"] == "x"


def test_gate_refuses_fabricated_resolved_field(tmp_path: Path) -> None:
    """A greenlight whose resolved carries a field the brief never recommended,
    with no nudge and no override, is REFUSED (the proving-run-#3 failure)."""
    _persist_brief(tmp_path, "run-1", "s1", {"resolved": {"cluster": "hoffman2"}})
    with pytest.raises(errors.SpecInvalid) as ei:
        _append(tmp_path, block="s1", response="y", resolved={"result_dir_template": "results/x"})
    msg = str(ei.value)
    assert "result_dir_template" in msg
    assert "provenance gate" in msg


def test_gate_passes_when_key_is_in_brief_as_resolved_key(tmp_path: Path) -> None:
    _persist_brief(tmp_path, "run-1", "s1", {"resolved": {"cluster": "hoffman2"}})
    out = _append(tmp_path, block="s1", response="y", resolved={"cluster": "carc"})
    assert out.record.resolved["cluster"] == "carc"


def test_gate_passes_when_key_named_in_ambiguity_value(tmp_path: Path) -> None:
    """The field name can surface as a VALUE (an ambiguity entry names the field
    it is about) — the walker collects string scalars, so this counts."""
    _persist_brief(
        tmp_path,
        "run-1",
        "s1",
        {"ambiguities": [{"field": "result_dir_template", "recommendation": None}]},
    )
    out = _append(tmp_path, block="s1", response="y", resolved={"result_dir_template": "results/x"})
    assert out.record.resolved["result_dir_template"] == "results/x"


def test_gate_passes_with_prior_nudge_naming_the_key(tmp_path: Path) -> None:
    """Path (b): a prior non-greenlight record whose text names the key."""
    _persist_brief(tmp_path, "run-1", "s1", {"resolved": {"cluster": "hoffman2"}})
    _append(tmp_path, block="s1", response="no — set the result_dir_template to results/foo")
    out = _append(
        tmp_path, block="s1", response="y", resolved={"result_dir_template": "results/foo"}
    )
    assert out.record.resolved["result_dir_template"] == "results/foo"


def test_gate_passes_with_provenance_overrides(tmp_path: Path) -> None:
    """Path (c): the key is listed in provenance.overrides."""
    _persist_brief(tmp_path, "run-1", "s1", {"resolved": {"cluster": "hoffman2"}})
    out = _append(
        tmp_path,
        block="s1",
        response="y",
        resolved={"result_dir_template": "results/x"},
        provenance={"overrides": ["result_dir_template"]},
    )
    assert out.record.resolved["result_dir_template"] == "results/x"


def test_gate_matches_short_block_name_against_persisted_long_name(tmp_path: Path) -> None:
    """Brief persisted under the long name; greenlight names the short form."""
    _persist_brief(tmp_path, "run-1", "submit-s1", {"resolved": {"cluster": "hoffman2"}})
    with pytest.raises(errors.SpecInvalid):
        _append(tmp_path, block="s1", response="y", resolved={"result_dir_template": "x"})


def test_gate_exempts_next_block_meta_key(tmp_path: Path) -> None:
    """next_block is a machine-owned routing token — never gated even though the
    brief never 'recommends' it."""
    _persist_brief(tmp_path, "run-1", "s1", {"resolved": {"cluster": "hoffman2"}})
    out = _append(tmp_path, block="s1", response="y", resolved={"cluster": "carc"})
    # The chain default injects next_block; the gate must not reject on it.
    assert out.record.resolved["next_block"] == "submit-s2"


def test_gate_does_not_fire_on_nudge_response(tmp_path: Path) -> None:
    """Only greenlights (response=='y') are gated; a nudge re-drafts."""
    _persist_brief(tmp_path, "run-1", "s1", {"resolved": {"cluster": "hoffman2"}})
    out = _append(tmp_path, block="s1", response="no", resolved={"result_dir_template": "x"})
    assert out.record.response == "no"


# ─── human-authorship gate (conduct rule 9 extension, proving run #4) ──────────

# Run #4's exact shape: the agent read the executor, invented the sweep,
# presented it as a recommendation, and the human's bare "y" laundered it
# into ``resolved`` as "caller-supplied".
_RUN4_PROPOSAL = "task_generator: items_x_seeds, 20 seeds (0-19), samples=1_000_000"
_RUN4_TASK_GENERATOR = {"kind": "items_x_seeds", "seeds": 20, "samples": 1_000_000}


def test_authorship_gate_refuses_bare_y_committing_proposed_task_generator(
    tmp_path: Path,
) -> None:
    """The proving-run-#4 fire path: bare 'y' + a task_generator that appears
    only in the agent's proposal is REFUSED, and nothing is journaled."""
    with pytest.raises(errors.SpecInvalid) as ei:
        _append(
            tmp_path,
            block="s1",
            response="y",
            proposal=_RUN4_PROPOSAL,
            resolved={"task_generator": _RUN4_TASK_GENERATOR},
        )
    msg = str(ei.value)
    assert "human-authorship gate" in msg
    assert "task_generator is human-authored" in msg
    assert "agent's proposal" in msg
    assert "20" in msg and "1000000" in msg  # the underivable tokens are named
    # The refused exchange never reaches the journal.
    result = read_decisions(
        experiment_dir=tmp_path,
        spec=ReadDecisionsInput.model_validate({"scope_kind": "run", "scope_id": "run-1"}),
    )
    assert result.count == 0


def test_authorship_gate_passes_when_response_states_the_sweep(tmp_path: Path) -> None:
    """Pass path (1): the human's own reply states the value tokens — including
    a magnitude suffix (1M) and a derived range endpoint (0-49 from 50)."""
    out = _append(
        tmp_path,
        block="s1",
        response="50 seeds at 1M samples",
        proposal=_RUN4_PROPOSAL,
        resolved={
            "task_generator": {
                "kind": "items_x_seeds",
                "seeds": 50,
                "seed_range": "0-49",
                "samples": 1_000_000,
            }
        },
    )
    assert out.record.resolved["task_generator"]["seeds"] == 50


def test_authorship_gate_passes_when_prior_nudge_stated_the_sweep(tmp_path: Path) -> None:
    """Pass paths (2)+(3): a prior human response in scope (here a nudge)
    stated the value, so a later bare 'y' commits it."""
    _append(tmp_path, block="s1", response="use 20 seeds at 1M samples each")
    out = _append(
        tmp_path,
        block="s1",
        response="y",
        proposal=_RUN4_PROPOSAL,
        resolved={"task_generator": _RUN4_TASK_GENERATOR},
    )
    assert out.record.resolved["task_generator"]["seeds"] == 20


def test_authorship_gate_ignores_non_required_caller_fields(tmp_path: Path) -> None:
    """Pass path (4): fields outside REQUIRED_CALLER_FIELDS are unaffected —
    a bare 'y' commits auto-resolvable fields exactly as before."""
    out = _append(tmp_path, block="s1", response="y", resolved={"cluster": "hoffman2"})
    assert out.record.resolved["cluster"] == "hoffman2"


def test_authorship_gate_skips_field_already_human_committed(tmp_path: Path) -> None:
    """Pass path (5): once the field was committed (through the gate) in a
    prior record, subsequent decisions restating it are unaffected."""
    _append(
        tmp_path,
        block="s1",
        response="20 seeds at 1M samples",
        resolved={"task_generator": _RUN4_TASK_GENERATOR},
    )
    out = _append(
        tmp_path,
        block="s2",
        response="y",
        resolved={"task_generator": _RUN4_TASK_GENERATOR},
    )
    assert out.record.resolved["task_generator"] == _RUN4_TASK_GENERATOR


def test_authorship_gate_refuses_bare_y_committing_goal(tmp_path: Path) -> None:
    """goal is free text: the decision that first commits it needs a non-bare
    reply (or human-text overlap) — a bare 'y' against a proposed goal is
    refused."""
    with pytest.raises(errors.SpecInvalid) as ei:
        _append(
            tmp_path,
            block="s1",
            response="y",
            proposal="goal: estimate pi via monte carlo",
            resolved={"goal": "estimate pi via monte carlo"},
        )
    assert "goal is human-authored" in str(ei.value)


def test_authorship_gate_passes_goal_on_non_bare_response(tmp_path: Path) -> None:
    """A substantive human reply on the committing decision passes goal."""
    out = _append(
        tmp_path,
        block="s1",
        response="yes — estimate pi to 4 decimal places",
        resolved={"goal": "estimate pi via monte carlo"},
    )
    assert out.record.resolved["goal"] == "estimate pi via monte carlo"


def test_authorship_gate_passes_goal_stated_in_prior_response(tmp_path: Path) -> None:
    """A prior human response that overlaps the goal text lets a bare 'y'
    commit it later."""
    _append(tmp_path, block="s1", response="the goal is to estimate pi via monte carlo")
    out = _append(
        tmp_path,
        block="s1",
        response="y",
        resolved={"goal": "estimate pi via monte carlo"},
    )
    assert out.record.resolved["goal"] == "estimate pi via monte carlo"


def _log_utterance(tmp_path: Path, text: str) -> None:
    """Simulate the harness-side UserPromptSubmit capture for *tmp_path*."""
    from hpc_agent.state.run_record import journal_dir
    from hpc_agent.state.utterances import append_utterance

    journal_dir(tmp_path)  # the namespace a real state write would have created
    assert append_utterance(tmp_path, text) is not None


def test_authorship_gate_refuses_fabricated_response_when_utterance_log_lacks_tokens(
    tmp_path: Path,
) -> None:
    """The laundering the lock exists for: with the capture hook installed,
    an agent-authored ``response`` quoting the sweep no longer counts — the
    tokens must derive from a HARNESS-logged utterance, and here the human
    never typed them."""
    _log_utterance(tmp_path, "hello, please check the cluster status")
    with pytest.raises(errors.SpecInvalid) as ei:
        _append(
            tmp_path,
            block="s1",
            # In journal-response mode this fabricated quote would PASS
            # (it states 20 and 1M) — the utterance log must outrank it.
            response="use 20 seeds at 1M samples",
            proposal=_RUN4_PROPOSAL,
            resolved={"task_generator": _RUN4_TASK_GENERATOR},
        )
    msg = str(ei.value)
    assert "human-authorship gate" in msg
    assert "task_generator is human-authored" in msg
    assert "harness-captured" in msg  # names the evidence source consulted
    assert "20" in msg and "1000000" in msg


def test_authorship_gate_passes_when_utterance_log_states_the_sweep(tmp_path: Path) -> None:
    """The intended flow under the hook: the human typed the sweep in a
    prompt (captured out-of-band), so a later bare 'y' commits it."""
    _log_utterance(tmp_path, "use 20 seeds at 1M samples each")
    out = _append(
        tmp_path,
        block="s1",
        response="y",
        proposal=_RUN4_PROPOSAL,
        resolved={"task_generator": _RUN4_TASK_GENERATOR},
    )
    assert out.record.resolved["task_generator"]["seeds"] == 20


def test_authorship_gate_utterance_mode_ignores_substantive_response_for_goal(
    tmp_path: Path,
) -> None:
    """In journal-response mode a non-bare reply commits a free-text goal;
    with a log present the response is agent-relayed and carries no
    authorship weight — the goal's words must overlap a logged utterance."""
    _log_utterance(tmp_path, "please submit the job")
    with pytest.raises(errors.SpecInvalid) as ei:
        _append(
            tmp_path,
            block="s1",
            response="yes — estimate pi to 4 decimal places",
            resolved={"goal": "estimate pi via monte carlo"},
        )
    assert "goal is human-authored" in str(ei.value)


def test_authorship_gate_passes_goal_overlapping_logged_utterance(tmp_path: Path) -> None:
    _log_utterance(tmp_path, "I want to estimate pi via monte carlo on hoffman2")
    out = _append(
        tmp_path,
        block="s1",
        response="y",
        resolved={"goal": "estimate pi via monte carlo"},
    )
    assert out.record.resolved["goal"] == "estimate pi via monte carlo"


def test_authorship_gate_old_schema_fail_open_does_not_apply_with_utterance_log(
    tmp_path: Path,
) -> None:
    """The old-schema escape hatch exists only because the journal lacks any
    human text; with a harness-captured log the stronger source exists, so a
    responseless prior journal no longer waves the commit through."""
    import json as _json

    from hpc_agent.state.decision_journal import decisions_path

    path = decisions_path(tmp_path, "run", "run-1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _json.dumps({"scope_kind": "run", "scope_id": "run-1", "block": "s1"}) + "\n",
        encoding="utf-8",
    )
    _log_utterance(tmp_path, "hello, please check the cluster status")
    with pytest.raises(errors.SpecInvalid):
        _append(
            tmp_path,
            block="s1",
            response="y",
            resolved={"task_generator": _RUN4_TASK_GENERATOR},
        )


def test_authorship_gate_fails_open_on_old_schema_journal(tmp_path: Path) -> None:
    """Fail-open: prior records exist but none carries a ``response`` key
    (old-schema journal) — there is no human text to derive from, so the
    gate does not fire."""
    import json

    from hpc_agent.state.decision_journal import decisions_path

    path = decisions_path(tmp_path, "run", "run-1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"scope_kind": "run", "scope_id": "run-1", "block": "s1"}) + "\n",
        encoding="utf-8",
    )
    out = _append(
        tmp_path,
        block="s1",
        response="y",
        resolved={"task_generator": _RUN4_TASK_GENERATOR},
    )
    assert out.record.resolved["task_generator"] == _RUN4_TASK_GENERATOR
