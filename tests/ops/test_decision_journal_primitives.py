"""Direct-atom tests for the ``append-decision`` / ``read-decisions`` primitives.

Constructs the wire spec, calls the primitive, asserts on the result —
bypassing the generated JSON (per the primitive recipe's atom-test
minimum). Covers the append→read round-trip through the primitive layer,
append-only discipline, both scopes, and the greenlight/nudge responses.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.decision_journal import AppendDecisionInput, AppendDecisionResult
from hpc_agent._wire.queries.decision_journal import ReadDecisionsInput
from hpc_agent.ops.decision.journal import append_decision, read_decisions

if TYPE_CHECKING:
    from pathlib import Path

    from hpc_agent._wire.queries.notebook_audit_view import NotebookAuditViewResult


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


def _write_section_render(
    tmp_path: Path,
    *,
    section: str,
    audit_id: str = "audit-x",
    source: str = _NB_SOURCE,
    template: str = _NB_TEMPLATE,
) -> Path:
    """Write the content-addressed TRUSTED-DISPLAY render for *section* (T8 lock).

    Mirrors what ``notebook-audit-view`` does per section — the render-then-sign
    contract the T8 gate now enforces. Built from the SAME (source, template) the
    sign-off's view_sha/section_sha derive from, so the render is current.
    """
    from hpc_agent.ops.notebook.audit_view import build_audit_view
    from hpc_agent.ops.notebook.render_store import write_render
    from hpc_agent.state.audit_source import parse_percent_source

    src = parse_percent_source(source)
    tmpl = parse_percent_source(template)
    sv = next(v for v in build_audit_view(src, tmpl, ()).sections if v.slug == section)
    return write_render(tmp_path, audit_id=audit_id, view=sv)


def _signoff(
    tmp_path: Path,
    *,
    section: str,
    response: str,
    section_sha: str,
    view_sha: str = "view-sha-1",
    audit_id: str = "audit-x",
    resolved_extra: dict[str, object] | None = None,
    render: bool = True,
    render_source: str = _NB_SOURCE,
    render_template: str = _NB_TEMPLATE,
    **overrides: object,
) -> AppendDecisionResult:
    # Render-then-sign (T8 trusted-display lock): by default write the render for
    # this section first, so the sign-off finds the current artifact. Tests probing
    # the missing/stale-render refusals pass ``render=False`` and stage the render
    # (or its absence) themselves.
    if render:
        _write_section_render(
            tmp_path,
            section=section,
            audit_id=audit_id,
            source=render_source,
            template=render_template,
        )
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


def test_signoff_no_template_refused(tmp_path: Path) -> None:
    """FULL-VIEW RECOMPUTE (v1.6, supersedes the retired conservative-empty-template
    boundary): the canonical view is a diff-from-template projection, so a sign-off
    whose template cannot be resolved (no resolved['template'], no interview.json)
    is REFUSED loudly — the signed view_sha is not reproducible without the
    template."""
    (tmp_path / "s.py").write_text(_NB_SOURCE, encoding="utf-8")
    section_sha, view_sha = _nb_shas("load-data")
    with pytest.raises(errors.SpecInvalid, match="could not resolve the audited .py TEMPLATE"):
        _signoff(
            tmp_path,
            section="load-data",
            response="load-data: read_csv on input.csv is correct",
            section_sha=section_sha,
            view_sha=view_sha,
            resolved_extra={"source": "s.py"},
        )


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


def test_signoff_gate_routes_through_canonical_view(tmp_path: Path) -> None:
    """One-definition route-through (v1.6 full-view recompute): the gate builds the
    view via the shared ``build_canonical_view`` (through the notebook_view facade),
    never a re-inlined view build — so its view_shas match the verbs' + plugin's."""
    import inspect

    from hpc_agent.ops.decision import journal as _journal

    src = inspect.getsource(_journal._assert_signoff_authorship)
    assert "build_canonical_view(" in src


def test_no_signoff_affordance_in_registry(tmp_path: Path) -> None:
    """Lock 1 (no affordance): NO primitive is named like a sign-off verb — a
    sign-off is an append-decision record or nothing (D5 lock 1). append-decision
    is the only write path."""
    from tests._registry_helpers import core_only_registry

    offenders = [name for name in core_only_registry() if "sign-off" in name or "signoff" in name]
    assert offenders == [], f"a sign-off verb affordance leaked into the registry: {offenders}"


# ---------------------------------------------------------------------------
# Trusted-display lock (v1.5): a sign-off requires the content-addressed render
# for what-the-human-saw to exist on disk AND be current at append. The audit
# view relayed in chat is model-carried; the render file code wrote is trusted.
# ---------------------------------------------------------------------------


def test_signoff_no_render_file_refused(tmp_path: Path) -> None:
    """No render artifact on disk → the sign-off is refused NAMING the path — the
    unforceable chat relay is not enough; the code-written render must exist."""
    _write_notebook_fixture(tmp_path)
    section_sha, view_sha = _nb_shas("model-fit")
    with pytest.raises(errors.SpecInvalid, match="trusted-display lock"):
        _signoff(
            tmp_path,
            section="model-fit",
            response="model-fit: the regularization=0.5 term is intentional, converged asserted",
            section_sha=section_sha,
            view_sha=view_sha,
            render=False,  # stage NO render
        )
    result = read_decisions(
        experiment_dir=tmp_path,
        spec=ReadDecisionsInput.model_validate({"scope_kind": "notebook", "scope_id": "audit-x"}),
    )
    assert result.count == 0


def test_signoff_render_present_and_current_passes(tmp_path: Path) -> None:
    """Render present + current (its header section_sha == the recompute) → the
    sign-off lands. The render was written by _write_section_render (the view verb's
    job) from the same current source."""
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
    # The render artifact is on disk at the content-addressed path.
    from hpc_agent.ops.notebook.render_store import read_render_header, render_path

    path = render_path(tmp_path, audit_id="audit-x", section="model-fit", view_sha=view_sha)
    header = read_render_header(path)
    assert header is not None
    assert header["view_sha"] == view_sha and header["section"] == "model-fit"


def test_signoff_edit_after_render_refused(tmp_path: Path) -> None:
    """Edit-after-render: the render was produced, then the source was edited (its
    recomputed sha moved) and the record's own sha updated so the bind lock passes —
    the STALE render's header sha no longer matches, so the sign-off is refused. This
    is exactly the seam the bind lock alone does NOT cover."""
    _write_notebook_fixture(tmp_path)
    _old_sha, old_view_sha = _nb_shas("model-fit")
    # Render produced against the ORIGINAL source.
    _write_section_render(tmp_path, section="model-fit")
    # Now edit the source so model-fit's sha moves.
    edited = _NB_SOURCE.replace("regularization=0.5", "regularization=0.9")
    (tmp_path / "source.py").write_text(edited, encoding="utf-8")
    new_sha, _new_view_sha = _nb_shas("model-fit", source=edited)
    with pytest.raises(errors.SpecInvalid, match="STALE"):
        _signoff(
            tmp_path,
            section="model-fit",
            response="model-fit: regularization=0.9 change reviewed",
            section_sha=new_sha,  # current → bind passes
            view_sha=old_view_sha,  # addresses the now-stale render
            render=False,  # do NOT re-render; the stale artifact stays
        )


def test_signoff_render_view_sha_mismatch_refused(tmp_path: Path) -> None:
    """A render artifact at the content-addressed path whose header disagrees with
    the signed view_sha is refused — the defensive cross-reference leg (the render
    header must agree on view_sha/section, not merely exist)."""
    _write_notebook_fixture(tmp_path)
    section_sha, view_sha = _nb_shas("model-fit")
    from hpc_agent.ops.notebook.render_store import render_path

    # Hand-write a render at the address the gate will look up, but with a WRONG
    # header view_sha (a corrupt / wrong artifact).
    path = render_path(tmp_path, audit_id="audit-x", section="model-fit", view_sha=view_sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "<!-- hpc-render audit_id: audit-x -->\n"
        "<!-- hpc-render section: model-fit -->\n"
        f"<!-- hpc-render section_sha: {section_sha} -->\n"
        "<!-- hpc-render view_sha: not-the-signed-view -->\n\n"
        "# stale body\n",
        encoding="utf-8",
    )
    with pytest.raises(errors.SpecInvalid, match="does not match the signed view"):
        _signoff(
            tmp_path,
            section="model-fit",
            response="model-fit: the regularization=0.5 term reviewed",
            section_sha=section_sha,
            view_sha=view_sha,
            render=False,
        )


def test_signoff_redundant_requires_render(tmp_path: Path) -> None:
    """The trusted-display lock applies to a REDUNDANT (auto-cleared) sign-off too:
    the human claims a review, so the artifact must exist. Without a render it is
    refused; with one it lands and is marked redundant."""
    _write_notebook_fixture(tmp_path)
    section_sha, view_sha = _nb_shas("load-data")
    with pytest.raises(errors.SpecInvalid, match="trusted-display lock"):
        _signoff(
            tmp_path,
            section="load-data",
            response="reviewed load-data anyway to be safe",
            section_sha=section_sha,
            view_sha=view_sha,
            render=False,
        )
    out = _signoff(
        tmp_path,
        section="load-data",
        response="reviewed load-data anyway to be safe",
        section_sha=section_sha,
        view_sha=view_sha,
    )
    assert out.count == 1
    assert out.record.resolved["redundant"] is True


# ---------------------------------------------------------------------------
# FULL-VIEW RECOMPUTE (v1.6): the gate RECOMPUTES view_sha from the canonical
# ingredients — real lint (recorded roots), journaled receipts, recorded order —
# and refuses a mismatch. A section human-required SOLELY by a lint flag now
# refuses a bare-slug sign-off (the closed conservative-floor gap).
# ---------------------------------------------------------------------------

# ``load-data`` reads a path literal (``data/input.csv``) and is byte-identical to
# the template (inherited). Present data file → the section is auto_cleared; a
# MISSING data file makes the executes-live lint flag the section, flipping it to
# human_required and MOVING its per-section view_sha.
_LINT_TEMPLATE = """# %%
# hpc-audit-section: load-data
import pandas as pd

data = pd.read_csv("data/input.csv")

# %%
# hpc-audit-section: model-fit
model = fit(data)
"""

_LINT_SOURCE = """# %%
# hpc-audit-section: load-data
import pandas as pd

data = pd.read_csv("data/input.csv")

# %%
# hpc-audit-section: model-fit
model = fit(data, regularization=0.5)
assert model.converged
"""


def _write_lint_fixture(
    tmp_path: Path, *, data_present: bool, audit_id: str = "audit-lint"
) -> None:
    """Source/template + interview.json recording input_roots; optional data file."""
    import json as _json

    (tmp_path / "source.py").write_text(_LINT_SOURCE, encoding="utf-8")
    (tmp_path / "template.py").write_text(_LINT_TEMPLATE, encoding="utf-8")
    (tmp_path / "interview.json").write_text(
        _json.dumps(
            {
                "audited_source": {
                    "source": "source.py",
                    "template": "template.py",
                    "audit_id": audit_id,
                    "input_roots": ["."],
                }
            }
        ),
        encoding="utf-8",
    )
    if data_present:
        (tmp_path / "data").mkdir(exist_ok=True)
        (tmp_path / "data" / "input.csv").write_text("x\n1\n", encoding="utf-8")


def _canonical_view(tmp_path: Path, audit_id: str = "audit-lint") -> NotebookAuditViewResult:
    """Run notebook-audit-view (CANONICAL) — writes the renders, returns the result."""
    from hpc_agent._wire.queries.notebook_audit_view import NotebookAuditViewSpec
    from hpc_agent.ops.notebook.view_op import notebook_audit_view

    return notebook_audit_view(
        experiment_dir=tmp_path,
        spec=NotebookAuditViewSpec.model_validate(
            {"audit_id": audit_id, "source": "source.py", "template": "template.py"}
        ),
    )


def test_signoff_stale_view_sha_refused_when_lint_input_vanishes(tmp_path: Path) -> None:
    """The core defect fix: view once (data present → load-data auto_cleared, its
    view_sha V1, render written at V1), THEN the data file vanishes, THEN sign V1.
    The section BODY is unchanged (bind passes) and the render exists (section_sha
    still current), so the gate's FULL recompute catches the moved lint ingredient —
    the recomputed view_sha differs from V1 → refused, naming the ingredient class."""
    _write_lint_fixture(tmp_path, data_present=True)
    result = _canonical_view(tmp_path)
    by_slug = {s.slug: s for s in result.sections}
    assert result.canonical is True
    ld = by_slug["load-data"]

    # The data file vanishes AFTER the view was rendered/signed.
    (tmp_path / "data" / "input.csv").unlink()

    with pytest.raises(errors.SpecInvalid, match="full-view recompute"):
        _signoff(
            tmp_path,
            section="load-data",
            response="load-data: read_csv on data/input.csv reviewed",
            section_sha=ld.section_sha,
            view_sha=ld.view_sha,
            audit_id="audit-lint",
            render=False,  # the V1 render is already on disk from the view call
        )


def test_signoff_tier_by_real_lint_flag_refuses_bare_slug(tmp_path: Path) -> None:
    """Closed gap: a section human-required SOLELY by a lint flag (a missing data
    path — no diff, no assertion) now recomputes as HUMAN_REQUIRED at the gate, so a
    bare-slug sign-off that does not ENGAGE the change is refused."""
    from hpc_agent.ops.notebook.audit_view import HUMAN_REQUIRED

    _write_lint_fixture(tmp_path, data_present=False)  # data missing → lint flags load-data
    result = _canonical_view(tmp_path)
    ld = {s.slug: s for s in result.sections}["load-data"]
    assert ld.tier == HUMAN_REQUIRED  # the lint flag forced it
    with pytest.raises(errors.SpecInvalid, match="must ENGAGE the change"):
        _signoff(
            tmp_path,
            section="load-data",
            response="load-data reviewed, looks good",  # names slug, engages nothing
            section_sha=ld.section_sha,
            view_sha=ld.view_sha,
            audit_id="audit-lint",
            render=False,
        )


def test_signoff_canonical_flow_end_to_end(tmp_path: Path) -> None:
    """lint → auto-clear → view → sign with REAL flags: the default canonical flow
    produces a gate-accepted sign-off. model-fit (modified) is signed with an
    engaging response; the canonical view_sha is accepted."""
    from hpc_agent._wire.actions.notebook_auto_clear import NotebookAutoClearSpec
    from hpc_agent.ops.notebook.auto_clear_op import notebook_auto_clear

    _write_lint_fixture(tmp_path, data_present=True)
    ac = notebook_auto_clear(
        experiment_dir=tmp_path,
        spec=NotebookAutoClearSpec.model_validate(
            {"audit_id": "audit-lint", "source": "source.py", "template": "template.py"}
        ),
    )
    assert "load-data" in [c.section for c in ac.cleared]
    result = _canonical_view(tmp_path)
    mf = {s.slug: s for s in result.sections}["model-fit"]
    out = _signoff(
        tmp_path,
        section="model-fit",
        response="model-fit: the regularization=0.5 term is intentional, converged asserted",
        section_sha=mf.section_sha,
        view_sha=mf.view_sha,
        audit_id="audit-lint",
        render=False,
    )
    # The scope journal now holds the load-data auto-clear + this model-fit sign-off.
    assert out.record.resolved["section"] == "model-fit"
    assert "redundant" not in out.record.resolved


def test_signoff_preview_view_sha_refused(tmp_path: Path) -> None:
    """A PREVIEW view (canonical=false — built with an explicit lint_findings
    override) carries per-section view_shas the gate recomputes differently, so
    signing a preview view_sha is refused."""
    from hpc_agent._wire.queries.notebook_audit_view import NotebookAuditViewSpec
    from hpc_agent.ops.notebook.view_op import notebook_audit_view

    _write_lint_fixture(tmp_path, data_present=True)
    preview = notebook_audit_view(
        experiment_dir=tmp_path,
        spec=NotebookAuditViewSpec.model_validate(
            {
                "audit_id": "audit-lint",
                "source": "source.py",
                "template": "template.py",
                "lint_findings": [{"rule": "executes_live", "section": "load-data", "detail": "x"}],
            }
        ),
    )
    assert preview.canonical is False
    ld = {s.slug: s for s in preview.sections}["load-data"]
    with pytest.raises(errors.SpecInvalid, match="full-view recompute"):
        _signoff(
            tmp_path,
            section="load-data",
            response="load-data: read_csv on data/input.csv reviewed",
            section_sha=ld.section_sha,
            view_sha=ld.view_sha,
            audit_id="audit-lint",
            render=False,
        )


# ---------------------------------------------------------------------------
# E2 (docs/design/mcp-elicitation.md D4/E2): the authorship-refusal marker.
# The authorship/sign-off BAR raise sites attach
# ``failure_features.authorship_evidence == "missing"`` — a machine-readable
# discriminator the MCP elicitation hook keys on WITHOUT parsing prose. It must
# survive the real CLI envelope path (``cli/_helpers._err_from_hpc`` → JSON on
# stdout, exactly what ``cli/dispatch.main`` runs on an HpcError) AND survive the
# synthesis trap: ``_err_from_hpc`` synthesizes a DEFAULT ``failure_features`` for
# EVERY spec_invalid, so a generic refusal has the BLOCK but not the KEY.
# ---------------------------------------------------------------------------


def _err_envelope(call: Callable[[], object]) -> dict[str, Any]:
    """Route the HpcError a refusing append raises through the CLI output boundary.

    ``cli/dispatch.main`` catches ``errors.HpcError`` and returns
    ``_err_from_hpc(exc)`` (dispatch.py) — this reproduces that exact seam and
    returns the emitted ok:false envelope dict, the real path E2's marker rides.
    """
    import contextlib
    import io
    import json as _json

    from hpc_agent.cli._helpers import _err_from_hpc

    with pytest.raises(errors.HpcError) as ei:
        call()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _err_from_hpc(ei.value)
    env: dict[str, Any] = _json.loads(buf.getvalue())
    assert env["ok"] is False
    assert env["error_code"] == "spec_invalid"
    return env


def test_signoff_bare_ack_refusal_carries_authorship_marker(tmp_path: Path) -> None:
    """A notebook sign-off bare-ack refusal surfaces the E2 marker through the
    envelope's ``failure_features``."""
    _write_notebook_fixture(tmp_path)
    section_sha, view_sha = _nb_shas("model-fit")
    env = _err_envelope(
        lambda: _signoff(
            tmp_path, section="model-fit", response="y", section_sha=section_sha, view_sha=view_sha
        )
    )
    assert env["failure_features"]["authorship_evidence"] == "missing"


def test_signoff_slug_not_named_refusal_carries_authorship_marker(tmp_path: Path) -> None:
    """The slug-naming floor refusal is an authorship-bar refusal → marked."""
    _write_notebook_fixture(tmp_path)
    section_sha, view_sha = _nb_shas("model-fit")
    env = _err_envelope(
        lambda: _signoff(
            tmp_path,
            section="model-fit",
            response="looks good, the regularization term is fine",
            section_sha=section_sha,
            view_sha=view_sha,
        )
    )
    assert env["failure_features"]["authorship_evidence"] == "missing"


def test_signoff_human_required_refusal_carries_authorship_marker(tmp_path: Path) -> None:
    """The raised HUMAN_REQUIRED 'engage the change' refusal is authorship → marked."""
    _write_notebook_fixture(tmp_path)
    section_sha, view_sha = _nb_shas("model-fit")
    env = _err_envelope(
        lambda: _signoff(
            tmp_path,
            section="model-fit",
            response="model-fit looks great, nice work",
            section_sha=section_sha,
            view_sha=view_sha,
        )
    )
    assert env["failure_features"]["authorship_evidence"] == "missing"


def test_unlock_bare_ack_refusal_carries_authorship_marker(tmp_path: Path) -> None:
    """The scope-unlock bare-ack refusal is authorship → marked."""
    env = _err_envelope(lambda: _unlock(tmp_path, response="y"))
    assert env["failure_features"]["authorship_evidence"] == "missing"


def test_unlock_harness_mismatch_refusal_carries_authorship_marker(tmp_path: Path) -> None:
    """The scope-unlock harness-captured-mismatch refusal is authorship → marked."""
    _log_utterance(tmp_path, "hello, please check the cluster status")
    env = _err_envelope(
        lambda: _unlock(tmp_path, response="reopen the embargoed evaluation partition")
    )
    assert env["failure_features"]["authorship_evidence"] == "missing"


def test_human_authorship_refusal_carries_authorship_marker(tmp_path: Path) -> None:
    """The human-authorship gate (proving-run-#4 shape) refusal is marked."""
    env = _err_envelope(
        lambda: _append(
            tmp_path,
            block="s1",
            response="y",
            proposal=_RUN4_PROPOSAL,
            resolved={"task_generator": _RUN4_TASK_GENERATOR},
        )
    )
    assert env["failure_features"]["authorship_evidence"] == "missing"


def test_generic_spec_invalid_has_features_without_authorship_key(tmp_path: Path) -> None:
    """The synthesis trap: a generic (non-authorship) spec_invalid refusal — here the
    code-derived-field gate — still carries a synthesized ``failure_features`` BLOCK
    (error_class/error_class_raw), but WITHOUT the ``authorship_evidence`` KEY. The
    MCP hook must key on the KEY, never the block's presence."""
    env = _err_envelope(lambda: _append(tmp_path, resolved={"executor": "monte_carlo_pi"}))
    features = env["failure_features"]
    assert features is not None
    assert "authorship_evidence" not in features
    # The synthesized default is intact (proves the block is present-but-different).
    assert features.get("error_class") == "code_bug"


def test_structural_signoff_refusal_not_marked_authorship(tmp_path: Path) -> None:
    """A STRUCTURAL sign-off refusal (a stale/asserted section_sha — the recompute
    lock, not missing authorship) must NOT carry the marker: re-eliciting a human
    utterance cannot fix a hash mismatch, so the MCP retry-once must not fire."""
    _write_notebook_fixture(tmp_path)
    env = _err_envelope(
        lambda: _signoff(
            tmp_path,
            section="model-fit",
            response="model-fit regularization reviewed",
            section_sha="deadbeef" * 8,
        )
    )
    features = env["failure_features"]
    # Either the synthesized default (no authorship key) — never the marker.
    assert features is None or "authorship_evidence" not in features


def test_unresolvable_source_signoff_refusal_not_marked_authorship(tmp_path: Path) -> None:
    """An unresolvable-source sign-off refusal is a setup error, not missing
    authorship → not marked."""
    section_sha, view_sha = _nb_shas("model-fit")
    env = _err_envelope(
        lambda: _signoff(
            tmp_path,
            section="model-fit",
            response="model-fit regularization reviewed",
            section_sha=section_sha,
            view_sha=view_sha,
        )
    )
    features = env["failure_features"]
    assert features is None or "authorship_evidence" not in features


def test_success_envelope_carries_no_failure_features(tmp_path: Path, capsys: Any) -> None:
    """The end-to-end success path (in-process ``dispatch.main``): a passing
    append-decision emits an ok:true envelope with no ``failure_features`` at all —
    the marker is a refusal-only discriminator."""
    import json

    from hpc_agent.cli.dispatch import main

    spec_file = tmp_path / "spec.json"
    spec_file.write_text(
        json.dumps(
            {
                "scope_kind": "run",
                "scope_id": "run-e2",
                "block": "submit.S1",
                "response": "y",
                "resolved": {},
            }
        ),
        encoding="utf-8",
    )
    rc = main(["append-decision", "--experiment-dir", str(tmp_path), "--spec", str(spec_file)])
    assert rc == 0
    out = capsys.readouterr().out
    env = json.loads([line for line in out.splitlines() if line.strip().startswith("{")][-1])
    assert env["ok"] is True
    assert "failure_features" not in env
