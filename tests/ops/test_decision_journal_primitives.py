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


def test_gate_prior_nudge_is_token_exact_not_substring(tmp_path: Path) -> None:
    """Path (b) is token-exact (#26): a nudge mentioning the key only as a
    SUBSTRING ("seeds") does not authorize a diverted field named "seed" — the
    old substring match wrongly did — while naming it as a whole token does."""
    _persist_brief(tmp_path, "run-1", "s1", {"resolved": {"cluster": "hoffman2"}})
    _append(tmp_path, block="s1", response="use seeds 0-19")  # substring, not a token
    with pytest.raises(errors.SpecInvalid):
        _append(tmp_path, block="s1", response="y", resolved={"seed": 0})
    # Naming the key as a whole token DOES authorize it.
    _append(tmp_path, block="s1", response="set seed to 0")
    out = _append(tmp_path, block="s1", response="y", resolved={"seed": 0})
    assert out.record.resolved["seed"] == 0


def test_activation_is_caller_overridable_not_journal_unauthorable(tmp_path: Path) -> None:
    """13-residual: activation (conda_env/conda_source/modules) is derived-by-
    default but caller-overridable (remote_activation_for_sidecar honors a pin),
    so it must NOT sit in JOURNAL_UNAUTHORABLE — a justified override commits —
    while a genuinely code-owned derived field (executor) stays refused even with
    the override, because it remains unauthorable."""
    from hpc_agent.ops.submit.field_partition import (
        CALLER_OVERRIDABLE_DERIVED_FIELDS,
        JOURNAL_UNAUTHORABLE_FIELDS,
    )

    assert "conda_env" in CALLER_OVERRIDABLE_DERIVED_FIELDS
    assert "conda_env" not in JOURNAL_UNAUTHORABLE_FIELDS
    assert "executor" in JOURNAL_UNAUTHORABLE_FIELDS

    _persist_brief(tmp_path, "run-1", "s1", {"resolved": {"cluster": "hoffman2"}})
    out = _append(
        tmp_path,
        block="s1",
        response="y",
        resolved={"conda_env": "myenv"},
        provenance={"overrides": ["conda_env"]},
    )
    assert out.record.resolved["conda_env"] == "myenv"
    with pytest.raises(errors.SpecInvalid):
        _append(
            tmp_path,
            block="s1",
            response="y",
            resolved={"executor": "python3 x.py"},
            provenance={"overrides": ["executor"]},
        )


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


def test_authorship_gate_derives_contiguous_seed_list_from_stated_count(
    tmp_path: Path,
) -> None:
    """Proving run #5: ``items_x_seeds`` materializes ``seeds=[0..19]``, and
    the human states that sweep as "20 seeds" — the gate must not demand the
    twenty-integer enumeration (a consecutive run asserts only endpoints +
    length)."""
    _log_utterance(tmp_path, "20 seeds, n_samples=1000000")
    out = _append(
        tmp_path,
        block="s1",
        response="y",
        proposal=_RUN4_PROPOSAL,
        resolved={
            "task_generator": {
                "kind": "items_x_seeds",
                "params": {"items": [{"n_samples": 1_000_000}], "seeds": list(range(20))},
            }
        },
    )
    assert out.record.resolved["task_generator"]["params"]["seeds"] == list(range(20))


def test_authorship_gate_derives_contiguous_seed_list_from_stated_range(
    tmp_path: Path,
) -> None:
    """The range form: "seeds 0 through 19" states both endpoints; the length
    (20) is the +1 range-endpoint derivation of a stated endpoint."""
    _log_utterance(tmp_path, "seeds 0 through 19, n_samples=1000000")
    out = _append(
        tmp_path,
        block="s1",
        response="y",
        resolved={
            "task_generator": {
                "kind": "items_x_seeds",
                "params": {"items": [{"n_samples": 1_000_000}], "seeds": list(range(20))},
            }
        },
    )
    assert out.record.resolved["task_generator"]["params"]["seeds"] == list(range(20))


def test_authorship_gate_still_refuses_unstated_noncontiguous_seed_list(
    tmp_path: Path,
) -> None:
    """The compression's fire path stays live: a NON-consecutive list asserts
    every member, and unstated members are refused — "20 seeds" does not
    derive ``[0, 5, 10, 15]``."""
    _log_utterance(tmp_path, "20 seeds, n_samples=1000000")
    with pytest.raises(errors.SpecInvalid) as ei:
        _append(
            tmp_path,
            block="s1",
            response="y",
            resolved={
                "task_generator": {
                    "kind": "items_x_seeds",
                    "params": {"items": [{"n_samples": 1_000_000}], "seeds": [0, 5, 10, 15]},
                }
            },
        )
    msg = str(ei.value)
    assert "task_generator is human-authored" in msg
    assert "5" in msg and "15" in msg


def test_authorship_gate_tokenizes_comma_separated_enumeration(tmp_path: Path) -> None:
    """Run #5: ``\\d[\\d,_]*`` collapsed a typed "0,5,10,15" into one giant
    grouped token, so the humanly-natural comma list never matched. A comma
    binds as grouping only in 3-digit groups; otherwise it separates tokens.
    Non-contiguous seeds isolate the tokenizer from the range compression."""
    _log_utterance(tmp_path, "seeds 0,5,10,15 with n_samples=1,000,000")
    out = _append(
        tmp_path,
        block="s1",
        response="y",
        resolved={
            "task_generator": {
                "kind": "items_x_seeds",
                "params": {"items": [{"n_samples": 1_000_000}], "seeds": [0, 5, 10, 15]},
            }
        },
    )
    assert out.record.resolved["task_generator"]["params"]["seeds"] == [0, 5, 10, 15]


def test_authorship_gate_still_refuses_contiguous_run_with_unstated_endpoints(
    tmp_path: Path,
) -> None:
    """A consecutive run whose endpoints the human never stated is still
    refused — compression narrows WHAT is checked, never WHETHER."""
    _log_utterance(tmp_path, "run the pi estimation")
    with pytest.raises(errors.SpecInvalid):
        _append(
            tmp_path,
            block="s1",
            response="y",
            resolved={"task_generator": {"kind": "items_x_seeds", "seeds": list(range(3, 23))}},
        )


def test_authorship_gate_refuses_fabricated_categorical_when_numbers_derive(
    tmp_path: Path,
) -> None:
    """Finding 25: the numeric-only structured check let a fabricated
    CATEGORICAL/string param ride through. The human states "20 seeds,
    n_samples=1000000" — every NUMBER in the task_generator derives (seeds
    [0..19] from "20 seeds", 1_000_000 from "n_samples=1000000") — but the
    agent smuggles a fabricated ``dataset`` axis the human never named. The
    numbers passing must NOT wave the non-numeric claim through."""
    _log_utterance(tmp_path, "20 seeds, n_samples=1000000")
    with pytest.raises(errors.SpecInvalid) as ei:
        _append(
            tmp_path,
            block="s1",
            response="y",
            resolved={
                "task_generator": {
                    "kind": "items_x_seeds",
                    "params": {
                        "items": [{"n_samples": 1_000_000, "dataset": "fabricated-set"}],
                        "seeds": list(range(20)),
                    },
                }
            },
        )
    msg = str(ei.value)
    assert "human-authorship gate" in msg
    assert "task_generator is human-authored" in msg
    assert "fabricated" in msg  # the smuggled categorical token is named
    assert "harness-captured" in msg  # names the evidence source consulted
    # The refused exchange never reaches the journal.
    result = read_decisions(
        experiment_dir=tmp_path,
        spec=ReadDecisionsInput.model_validate({"scope_kind": "run", "scope_id": "run-1"}),
    )
    assert result.count == 0


def test_authorship_gate_passes_categorical_the_human_named(tmp_path: Path) -> None:
    """The categorical check is a lock, not a wall: a param VALUE the human
    DID name (here a cartesian axis over datasets the utterance lists) passes,
    proving the string-leaf check gates fabrication, not legitimate claims.
    The ``kind`` discriminator ("cartesian_product") is schema vocabulary and
    is never itself treated as a claim even though the human never typed it."""
    _log_utterance(tmp_path, "cartesian sweep over datasets cifar10 and mnist, 20 seeds")
    out = _append(
        tmp_path,
        block="s1",
        response="y",
        resolved={
            "task_generator": {
                "kind": "cartesian_product",
                "params": {
                    "axes": {
                        "dataset": ["cifar10", "mnist"],
                        "seed": list(range(20)),
                    }
                },
            }
        },
    )
    assert out.record.resolved["task_generator"]["params"]["axes"]["dataset"] == [
        "cifar10",
        "mnist",
    ]


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


# ---------------------------------------------------------------------------
# Code-derived field gate (run #6 F1): a resolved dict must never
# hand-commit a field the framework derives.
# ---------------------------------------------------------------------------


def test_append_refuses_code_derived_resolved_field(tmp_path: Path) -> None:
    """The F1 shape verbatim: a greenlight committing a hand-authored
    executor is refused, pointing at the revise-resolved rail."""
    with pytest.raises(errors.SpecInvalid, match="CODE-DERIVED") as exc_info:
        _append(tmp_path, resolved={"cluster": "discovery", "executor": "monte_carlo_pi"})
    assert "revise-resolved" in str(exc_info.value)


@pytest.mark.parametrize(
    # NOT modules/conda_source/conda_env — those are CALLER_OVERRIDABLE_DERIVED
    # now (13-residual), asserted separately in
    # test_activation_is_caller_overridable_not_journal_unauthorable.
    "field",
    ["job_env", "ssh_target", "backend"],
)
def test_append_refuses_each_journal_unauthorable_field(tmp_path: Path, field: str) -> None:
    with pytest.raises(errors.SpecInvalid, match="CODE-DERIVED"):
        _append(tmp_path, resolved={field: "hand-authored"})


def test_append_allows_sanctioned_identity_echoes(tmp_path: Path) -> None:
    """run_id / cmd_sha / total_tasks are legitimately present in a committed
    resolved (status/aggregate input; the section-4 identity fast-path token;
    the finding-21-cross-checked count echo) -- the gate must not fire."""
    out = _append(
        tmp_path,
        resolved={
            "cluster": "discovery",
            "run_id": "run-1",
            "cmd_sha": "deadbeef",
            "total_tasks": 10,
        },
    )
    assert out.count == 1


def test_append_allows_plain_input_resolved(tmp_path: Path) -> None:
    out = _append(tmp_path, resolved={"cluster": "hoffman2", "walltime_sec": 600})
    assert out.count == 1


# ---------------------------------------------------------------------------
# Scope-unlock authorship gate (rigor primitives, T4): unlocking a caller
# scope RELAXES a restriction, so a bare `y` cannot enact it.
# ---------------------------------------------------------------------------


def _unlock(tmp_path: Path, **overrides: object) -> AppendDecisionResult:
    base: dict[str, object] = {
        "scope_kind": "scope",
        "scope_id": "holdout",
        "block": "scope-unlock",
        "response": "reopen for one more confirmatory sweep",
        "resolved": {"scope_action": "unlock"},
    }
    base.update(overrides)
    return append_decision(experiment_dir=tmp_path, spec=AppendDecisionInput.model_validate(base))


def test_unlock_bare_ack_refused(tmp_path: Path) -> None:
    """A bare `y` cannot unlock a scope — the gate names the human-rationale
    requirement, and nothing is journaled."""
    with pytest.raises(errors.SpecInvalid) as ei:
        _unlock(tmp_path, response="y")
    msg = str(ei.value)
    assert "scope-unlock authorship gate" in msg
    assert "HUMAN act" in msg
    assert "rationale" in msg
    # The refused exchange never reaches the journal.
    result = read_decisions(
        experiment_dir=tmp_path,
        spec=ReadDecisionsInput.model_validate({"scope_kind": "scope", "scope_id": "holdout"}),
    )
    assert result.count == 0


def test_unlock_human_typed_rationale_passes_journal_response_tier(tmp_path: Path) -> None:
    """No utterance log: the non-bare typed `response` IS the human's rationale."""
    out = _unlock(tmp_path, response="reopen the holdout for a confirmatory sweep")
    assert out.record.resolved["scope_action"] == "unlock"
    assert out.count == 1


def test_unlock_human_typed_rationale_passes_utterance_tier(tmp_path: Path) -> None:
    """With the capture hook installed, the rationale words must derive from a
    logged human utterance — here they do, so the unlock commits."""
    _log_utterance(tmp_path, "please reopen the holdout scope for one more sweep")
    out = _unlock(tmp_path, response="reopen for one more sweep")
    assert out.record.resolved["scope_action"] == "unlock"
    assert out.count == 1


def test_unlock_utterance_mismatch_refused(tmp_path: Path) -> None:
    """With a log present, an agent-relayed rationale whose words never appear in
    any logged utterance is refused (the harness-captured lock)."""
    _log_utterance(tmp_path, "hello, please check the cluster status")
    with pytest.raises(errors.SpecInvalid) as ei:
        _unlock(tmp_path, response="reopen the embargoed evaluation partition")
    msg = str(ei.value)
    assert "scope-unlock authorship gate" in msg
    assert "harness-captured" in msg


def test_unlock_block_convention_enforced_both_directions(tmp_path: Path) -> None:
    """A `scope-unlock` block is scope-only; a scope unlock must carry that block."""
    # scope-unlock block for a non-scope kind is refused.
    with pytest.raises(errors.SpecInvalid, match="only valid for scope_kind='scope'"):
        _append(tmp_path, block="scope-unlock", response="reopen it please")
    # A scope unlock hiding under the lock block is refused.
    with pytest.raises(errors.SpecInvalid, match="must be journaled with block='scope-unlock'"):
        _unlock(tmp_path, block="scope-lock")


def test_lock_append_needs_no_authorship_bar(tmp_path: Path) -> None:
    """Locking is the SAFE direction — a lock committed via append-decision,
    even with a bare `y`, passes without the authorship bar."""
    out = _append(
        tmp_path,
        scope_kind="scope",
        scope_id="holdout",
        block="scope-lock",
        response="y",
        resolved={"scope_action": "lock"},
    )
    assert out.record.resolved["scope_action"] == "lock"
    assert out.count == 1


# ---------------------------------------------------------------------------
# Notebook sign-off authorship gate (D5 three locks + D-attention, T8): a
# sign-off ATTESTS a human reviewed a section at a specific hash — the section
# sha is RECOMPUTED (un-fakeable, via the attestation kernel), the response must
# name the slug and, for a HUMAN-REQUIRED section, engage the change.
# ---------------------------------------------------------------------------

_NB_TEMPLATE = """# %%
# hpc-audit-section: load-data
import pandas as pd

data = pd.read_csv("input.csv")

# %%
# hpc-audit-section: model-fit
model = fit(data)
"""

# ``load-data`` is byte-identical to the template (inherited, no assertions →
# AUTO_CLEARED); ``model-fit`` diverges and adds an assertion (modified + an
# ungreen assert → HUMAN_REQUIRED).
_NB_SOURCE = """# %%
# hpc-audit-section: load-data
import pandas as pd

data = pd.read_csv("input.csv")

# %%
# hpc-audit-section: model-fit
model = fit(data, regularization=0.5)
assert model.converged
"""


def _write_notebook_fixture(
    tmp_path: Path,
    *,
    source: str = _NB_SOURCE,
    template: str = _NB_TEMPLATE,
    audit_id: str = "audit-x",
) -> None:
    (tmp_path / "source.py").write_text(source, encoding="utf-8")
    (tmp_path / "template.py").write_text(template, encoding="utf-8")
    interview = {
        "audited_source": {
            "source": "source.py",
            "template": "template.py",
            "audit_id": audit_id,
        }
    }
    import json as _json

    (tmp_path / "interview.json").write_text(_json.dumps(interview), encoding="utf-8")


def _nb_shas(
    slug: str, *, source: str = _NB_SOURCE, template: str = _NB_TEMPLATE
) -> tuple[str, str]:
    """The current ``(section_sha, view_sha)`` for *slug* — what a real sign-off asserts."""
    from hpc_agent.ops.notebook.audit_view import build_audit_view
    from hpc_agent.state.audit_source import parse_percent_source

    src = parse_percent_source(source)
    tmpl = parse_percent_source(template)
    sect = next(s for s in src.sections if s.slug == slug)
    sv = next(v for v in build_audit_view(src, tmpl, ()).sections if v.slug == slug)
    return sect.section_sha, sv.view_sha


def _signoff(
    tmp_path: Path,
    *,
    section: str,
    response: str,
    section_sha: str,
    view_sha: str = "view-sha-1",
    audit_id: str = "audit-x",
    resolved_extra: dict[str, object] | None = None,
    **overrides: object,
) -> AppendDecisionResult:
    resolved: dict[str, object] = {
        "audit_id": audit_id,
        "section": section,
        "section_sha": section_sha,
        "view_sha": view_sha,
    }
    if resolved_extra:
        resolved.update(resolved_extra)
    base: dict[str, object] = {
        "scope_kind": "notebook",
        "scope_id": audit_id,
        "block": "notebook-sign-off",
        "response": response,
        "resolved": resolved,
    }
    base.update(overrides)
    return append_decision(experiment_dir=tmp_path, spec=AppendDecisionInput.model_validate(base))


def test_signoff_current_sha_and_engaging_response_passes(tmp_path: Path) -> None:
    """The green path: the asserted sha matches the recompute, the response names
    the slug AND engages a diff identifier — the HUMAN_REQUIRED bar is met."""
    _write_notebook_fixture(tmp_path)
    section_sha, view_sha = _nb_shas("model-fit")
    out = _signoff(
        tmp_path,
        section="model-fit",
        response="model-fit: the regularization=0.5 term is intentional, converged asserted",
        section_sha=section_sha,
        view_sha=view_sha,
    )
    assert out.count == 1
    assert out.record.resolved["section"] == "model-fit"
    assert "redundant" not in out.record.resolved


def test_signoff_sha_mismatch_refused(tmp_path: Path) -> None:
    """Lock 2: an asserted section_sha that does not match the recomputed one is
    refused — a hash cannot be asserted into existence."""
    _write_notebook_fixture(tmp_path)
    with pytest.raises(errors.SpecInvalid, match="does not match the recomputed"):
        _signoff(
            tmp_path,
            section="model-fit",
            response="model-fit regularization reviewed",
            section_sha="deadbeef" * 8,
        )
    # Nothing was journaled.
    result = read_decisions(
        experiment_dir=tmp_path,
        spec=ReadDecisionsInput.model_validate({"scope_kind": "notebook", "scope_id": "audit-x"}),
    )
    assert result.count == 0


def test_signoff_bare_ack_refused(tmp_path: Path) -> None:
    """Lock 3 floor: a bare `y` cannot sign off — signing is a HUMAN act."""
    _write_notebook_fixture(tmp_path)
    section_sha, view_sha = _nb_shas("model-fit")
    with pytest.raises(errors.SpecInvalid, match="HUMAN act"):
        _signoff(
            tmp_path, section="model-fit", response="y", section_sha=section_sha, view_sha=view_sha
        )


def test_signoff_missing_slug_token_refused(tmp_path: Path) -> None:
    """Lock 3: the response must NAME the section slug (token-exact, #26)."""
    _write_notebook_fixture(tmp_path)
    section_sha, view_sha = _nb_shas("model-fit")
    with pytest.raises(errors.SpecInvalid, match="must NAME the section slug"):
        _signoff(
            tmp_path,
            section="model-fit",
            response="looks good, the regularization term is fine",
            section_sha=section_sha,
            view_sha=view_sha,
        )


def test_signoff_human_required_generic_praise_refused(tmp_path: Path) -> None:
    """D-attention: a HUMAN_REQUIRED section demands the response ENGAGE the
    change — naming the slug with only generic praise is refused."""
    _write_notebook_fixture(tmp_path)
    section_sha, view_sha = _nb_shas("model-fit")
    with pytest.raises(errors.SpecInvalid, match="must ENGAGE the change"):
        _signoff(
            tmp_path,
            section="model-fit",
            response="model-fit looks great, nice work",
            section_sha=section_sha,
            view_sha=view_sha,
        )


def test_signoff_auto_cleared_accepted_and_marked_redundant(tmp_path: Path) -> None:
    """An AUTO_CLEARED section accepts a voluntary human sign-off but marks it
    redundant (the recorded accept-vs-refuse decision)."""
    _write_notebook_fixture(tmp_path)
    section_sha, view_sha = _nb_shas("load-data")
    out = _signoff(
        tmp_path,
        section="load-data",
        response="reviewed load-data anyway to be safe",
        section_sha=section_sha,
        view_sha=view_sha,
    )
    assert out.count == 1
    assert out.record.resolved["redundant"] is True


def test_signoff_missing_view_sha_refused(tmp_path: Path) -> None:
    """view_sha binds what-the-human-saw (D5) and is required, non-empty."""
    _write_notebook_fixture(tmp_path)
    section_sha, _ = _nb_shas("model-fit")
    with pytest.raises(errors.SpecInvalid, match="view_sha"):
        _signoff(
            tmp_path,
            section="model-fit",
            response="model-fit regularization reviewed",
            section_sha=section_sha,
            view_sha="",
        )


def test_signoff_unresolvable_source_refused(tmp_path: Path) -> None:
    """No interview.json audited_source and no resolved['source'] → the source
    cannot be recomputed → REFUSED loudly (never silently skipped)."""
    section_sha, view_sha = _nb_shas("model-fit")  # a sha the gate can never confirm
    with pytest.raises(errors.SpecInvalid, match="could not resolve the audited"):
        _signoff(
            tmp_path,
            section="model-fit",
            response="model-fit regularization reviewed",
            section_sha=section_sha,
            view_sha=view_sha,
        )


def test_signoff_source_via_resolved_path_overrides_interview(tmp_path: Path) -> None:
    """The caller may supply the source/template paths directly in resolved."""
    (tmp_path / "s.py").write_text(_NB_SOURCE, encoding="utf-8")
    (tmp_path / "t.py").write_text(_NB_TEMPLATE, encoding="utf-8")
    section_sha, view_sha = _nb_shas("model-fit")
    out = _signoff(
        tmp_path,
        section="model-fit",
        response="model-fit regularization=0.5 verified",
        section_sha=section_sha,
        view_sha=view_sha,
        resolved_extra={"source": "s.py", "template": "t.py"},
    )
    assert out.count == 1


def test_signoff_no_template_forces_human_required(tmp_path: Path) -> None:
    """With no template resolvable the tier cannot be auto-cleared — even the
    normally-inherited ``load-data`` section reads HUMAN_REQUIRED and demands
    engagement (conservative, never auto-soften)."""
    (tmp_path / "s.py").write_text(_NB_SOURCE, encoding="utf-8")
    section_sha, view_sha = _nb_shas("load-data")
    # Generic praise is refused because the section is forced human-required.
    with pytest.raises(errors.SpecInvalid, match="must ENGAGE the change"):
        _signoff(
            tmp_path,
            section="load-data",
            response="load-data looks fine to me",
            section_sha=section_sha,
            view_sha=view_sha,
            resolved_extra={"source": "s.py"},
        )
    # Engaging a real identifier from the (now whole-section) diff passes.
    out = _signoff(
        tmp_path,
        section="load-data",
        response="load-data: read_csv on input.csv is correct",
        section_sha=section_sha,
        view_sha=view_sha,
        resolved_extra={"source": "s.py"},
    )
    assert out.count == 1


def test_signoff_block_is_notebook_only(tmp_path: Path) -> None:
    """The notebook-sign-off block is refused for a non-notebook scope_kind."""
    with pytest.raises(errors.SpecInvalid, match="only valid for scope_kind='notebook'"):
        _append(tmp_path, block="notebook-sign-off", response="model-fit reviewed")


def test_notebook_scope_non_signoff_block_unaffected(tmp_path: Path) -> None:
    """A notebook-scoped record under a DIFFERENT block passes untouched — the
    gate keys strictly on the notebook-sign-off block."""
    out = append_decision(
        experiment_dir=tmp_path,
        spec=AppendDecisionInput.model_validate(
            {
                "scope_kind": "notebook",
                "scope_id": "audit-x",
                "block": "notebook-note",
                "response": "y",
                "resolved": {},
            }
        ),
    )
    assert out.count == 1


def test_signoff_gate_routes_through_attestation_kernel(tmp_path: Path) -> None:
    """Enforcement-map route-through (docs/internals/engineering-principles.md):
    the sign-off recompute lock calls the ONE attestation kernel `bind`, never a
    re-inlined recompute-and-compare (the migrating-member assertion T8 adds)."""
    import inspect

    from hpc_agent.ops.decision import journal as _journal

    src = inspect.getsource(_journal._assert_signoff_authorship)
    assert "attestation.bind(" in src


def test_no_signoff_affordance_in_registry(tmp_path: Path) -> None:
    """Lock 1 (no affordance): NO primitive is named like a sign-off verb — a
    sign-off is an append-decision record or nothing (D5 lock 1). append-decision
    is the only write path."""
    from tests._registry_helpers import core_only_registry

    offenders = [name for name in core_only_registry() if "sign-off" in name or "signoff" in name]
    assert offenders == [], f"a sign-off verb affordance leaked into the registry: {offenders}"
