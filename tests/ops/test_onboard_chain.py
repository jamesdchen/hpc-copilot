"""Wave P2.c — the ``onboard`` chain, the S1 meld, and the ONE-``y`` gate census.

Six things this file pins, each named by the unit's own scope:

1. **chain derivations** — ``ORDER["onboard"]`` and everything derived off it
   (``WORKFLOW_OF`` / ``block_index`` / ``chain_successor``), including that
   re-homing ``audit-handoff`` out of its P2.b single-member family did not move
   any block_index the §4 field-change routing compares.
2. **the handoff's stage split** — the one field (``goal``) that decides whether
   the chain advances or parks, and that the park carries a composed ask.
3. **the wrap block surface** — the AGENT/HUMAN split of the wrapper-argv park,
   computed from the extraction evidence the composite already carries.
4. **the S1 meld** — ``notebook-status``'s ``sections_pending`` brief carries an
   ``s1_preview`` when an interview exists, and is ABSENT AND HONEST when not.
5. **queue intake** — one item per run_id, idempotent on re-entry.
6. **the ONE-``y`` end-to-end** — a live run-scoped standing consent covers the
   WHOLE submit chain (S2 → S3 → S4 → aggregate-run) with exactly one human act.

TOY VOCABULARY ONLY (the ``test_overnight_consent`` idiom). Hermetic: journal
home + clusters.yaml under ``tmp_path``; no SSH, no scheduler, no subprocess.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.decision_journal import AppendDecisionInput
from hpc_agent.infra import block_chain
from hpc_agent.ops import overnight
from hpc_agent.ops.block_gate import assert_greenlit_or_consented
from hpc_agent.ops.decision.journal import append_decision
from hpc_agent.state.runs import read_run_cluster, read_run_cmd_sha, write_run_sidecar
from hpc_agent.state.utterances import append_utterance

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "widget-run-p2c"
_CMD_SHA = "c0ffee11223344556677"
_CLUSTER = "carc"


# ── 1. chain derivations ──────────────────────────────────────────────────────


def test_onboard_order_and_derivations() -> None:
    assert block_chain.ORDER["onboard"] == [
        "audit-handoff",
        "wrap-entry-point-auto",
        "interview",
    ]
    for verb in block_chain.ORDER["onboard"]:
        assert block_chain.workflow_of(verb) == "onboard"
    # The re-home did NOT move audit-handoff's position: it was index 0 of its
    # P2.b single-member family and is index 0 of the onboard chain, so every
    # §4 field-change routing comparison sees the same number it always did.
    assert block_chain.block_index("audit-handoff") == 0
    assert block_chain.chain_successor("audit-handoff") == "wrap-entry-point-auto"
    assert block_chain.chain_successor("wrap-entry-point-auto") == "interview"
    # The chain's own last block has no chain-forward successor; the EXIT into
    # the submit family is a stage-keyed edge, not a linear touchpoint.
    assert block_chain.chain_successor("interview") is None
    assert block_chain.successor_verb("interview", "interviewed") == "submit-s1"
    # Nothing in the onboard chain reaches a cluster, so nothing in it is gated.
    assert not any(block_chain.is_gated(v) for v in block_chain.ORDER["onboard"])
    # Every onboard verb has a FINITE non-watch deadline (the unbounded
    # parent-wait class): a verb missing from DEADLINE_SECONDS would silently
    # inherit the 24h watch-class default.
    for verb in block_chain.ORDER["onboard"]:
        assert verb in block_chain.DEADLINE_SECONDS
        assert verb not in block_chain.WATCH_VERBS


def test_wrapper_argv_park_is_the_chain_s_second_agent_park() -> None:
    """The AGENT/HUMAN split is a registry edit, not a runtime sniff."""
    assert block_chain.park_actor("wrap-entry-point-auto", "needs_wrapper_argv") == "agent"
    assert (
        block_chain.park_actor("wrap-entry-point-auto", "needs_wrapper_argv_unsupported") == "human"
    )
    assert block_chain.park_actor("wrap-entry-point-auto", "needs_pick") == "human"
    assert block_chain.park_actor("audit-handoff", "needs_intent") == "human"


def test_onboard_composers_are_idempotent() -> None:
    """Re-composing an already-composed spec at park time is a no-op.

    The onboard composers read CARRIER keys that are not fields of the spec they
    produce, so ``park``'s re-application would refuse a spec it had just built
    unless each composer recognises its own output. Recognition is a POSITIVE
    shape test, which this pins in both directions.
    """
    hint = block_chain.next_block_hint(
        "wrap-entry-point-auto",
        "onboarded",
        why="t",
        interview_spec={
            "goal": "g",
            "task_count": 2,
            "task_generator": {"kind": "enumerated", "params": {"items": [{"a": 1}, {"a": 2}]}},
            "produced_by": {"kind": "human", "operator": "op"},
        },
        audited_source={"source": "s.py", "audit_id": "a", "template": "t.py"},
    )
    assert hint is not None
    once = hint["spec_hint"]
    twice = block_chain.compose_successor_spec("interview", spec_hint=once)
    assert twice == once
    # …and a genuinely empty hint still REFUSES rather than passing through.
    with pytest.raises(block_chain.SuccessorSpecIncomplete):
        block_chain.compose_successor_spec("interview", spec_hint={})


# ── 2. the handoff's stage split ──────────────────────────────────────────────


def _audit_source(experiment_dir: Path) -> None:
    (experiment_dir / "widget.py").write_text(
        "from hpc_agent import register_run\n\n\n@register_run\ndef widget():\n    return 1\n",
        encoding="utf-8",
    )
    (experiment_dir / "tmpl.py").write_text("# %% [widget]\n", encoding="utf-8")


def _handoff(experiment_dir: Path, monkeypatch: pytest.MonkeyPatch, *, goal: str | None) -> Any:
    from hpc_agent._wire.queries.audit_handoff import AuditHandoffSpec
    from hpc_agent.ops.notebook import audit_handoff_op

    monkeypatch.setattr(audit_handoff_op, "read_audit_intent", lambda *_a, **_k: (goal, ["radius"]))
    return audit_handoff_op.audit_handoff(
        experiment_dir=experiment_dir,
        spec=AuditHandoffSpec(audit_id="aud1", source="widget.py", template="tmpl.py"),
    )


def test_handoff_advances_when_the_goal_was_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _audit_source(tmp_path)
    result = _handoff(tmp_path, monkeypatch, goal="measure widget throughput")
    assert result.stage_reached == "placeholders_resolvable"
    assert result.needs_decision is False
    assert result.brief == {}
    assert result.next_block is not None
    assert result.next_block["verb"] == "wrap-entry-point-auto"
    # The hint IS wrap's complete input spec (the chain is ungated), carrying the
    # audited source as the entry point and the opaque provenance block.
    hint = result.next_block["spec_hint"]
    assert hint["entry_point_path"] == "widget.py"
    assert hint["goal"] == "measure widget throughput"
    assert hint["audited_source"]["audit_id"] == "aud1"


def test_handoff_parks_with_one_composed_ask_when_no_goal_was_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _audit_source(tmp_path)
    result = _handoff(tmp_path, monkeypatch, goal=None)
    assert result.stage_reached == "needs_intent"
    assert result.needs_decision is True
    assert result.next_block is None
    brief = result.brief
    assert brief["missing_fields"] == ["goal"]
    # The ask carries what the projection ALREADY derived, so the seam never
    # re-asks for something it holds…
    assert brief["recorded_task_axes"] == ["radius"]
    assert brief["disclosures"]
    # …and it does NOT propose a goal, even though it has the axis names: an
    # invented goal becomes a journaled fact through the interview.
    assert "measure" not in json.dumps(brief).lower()


# ── 4. the S1 meld ────────────────────────────────────────────────────────────


def test_s1_preview_is_absent_and_honest_without_an_interview(tmp_path: Path) -> None:
    from hpc_agent.ops.s1_meld import compose_s1_preview

    assert compose_s1_preview(tmp_path) is None


def test_s1_preview_rides_the_signoff_brief_when_an_interview_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hpc_agent.ops.s1_meld import compose_s1_preview

    monkeypatch.setattr(
        "hpc_agent.state.interview_doc.iter_interview_docs",
        lambda _d: iter(
            [
                {
                    "goal": "measure widget throughput",
                    "task_generator": {
                        "kind": "enumerated",
                        "params": {"items": [{"a": 1}, {"a": 2}]},
                    },
                    "entry_point": {"kind": "register_run", "run_name": "widget"},
                    "cmd_sha": "abcdef0123456789",
                }
            ]
        ),
    )
    preview = compose_s1_preview(tmp_path)
    assert preview is not None
    assert preview["checked"] is True
    # It is S1's OWN walk, run read-only: the resolved map is real…
    assert isinstance(preview["resolved"], dict)
    assert isinstance(preview["ambiguities"], list)
    # …and the R-a disclosure states the bar, read from the grant vocabulary's
    # one home rather than restated here.
    assert preview["standing_consent"]["bar"] == overnight.STANDING_CONSENT_BAR
    assert preview["interview_cmd_sha12"] == "abcdef012345"
    # READ-ONLY: nothing was minted (no run sidecars, no journal).
    assert not (tmp_path / ".hpc" / "runs").exists()


def test_the_meld_is_keyed_on_the_stage_and_notebook_status_has_only_two() -> None:
    """Why no test can tell `stage == sections_pending` from `needs_decision` — yet.

    ``notebook-status`` has exactly TWO terminators and only one of them asks, so
    the two conditions are equivalent TODAY and a mutation swapping them survives
    every battery. That is a fact about the block, not a hole in the guard — but
    an unstated equivalence is how the broader condition quietly becomes wrong
    later, so it is pinned here instead of assumed.

    The meld keys on the STAGE because ``needs_decision`` is a derived flag: a
    future third terminator that also parks — for a question with nothing to do
    with a submit — would silently start carrying an S1 preview into a brief that
    never asked for one. This assertion is what forces that decision to be made on
    purpose: add a terminator and this goes red until someone classifies it.
    """
    from typing import get_args

    from hpc_agent._wire.queries.notebook_status import NotebookStatusResult

    stages = NotebookStatusResult.model_fields["stage_reached"].annotation
    assert set(get_args(stages)) == {"audit_passed", "sections_pending"}, (
        "notebook-status grew a terminator. The S1 meld is keyed on "
        "`sections_pending`; decide DELIBERATELY whether the new stage should "
        "carry a submit preview, then update this pin."
    )


def test_signoff_park_brief_carries_the_meld(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wiring seat: the meld reaches the brief the human actually reads."""
    from hpc_agent.ops.notebook import status_op

    monkeypatch.setattr(status_op, "compose_s1_preview", None, raising=False)
    monkeypatch.setattr(
        "hpc_agent.ops.s1_meld.compose_s1_preview", lambda _d: {"checked": True, "clean": True}
    )
    brief: dict[str, Any] = {}
    status_op._attach_s1_preview(tmp_path, brief)
    assert brief["s1_preview"] == {"checked": True, "clean": True}


# ── 5. queue intake ───────────────────────────────────────────────────────────


def test_chain_queue_intake_is_one_item_per_run_idempotent(tmp_path: Path) -> None:
    from hpc_agent.state.queue_intake import read_intake_items
    from hpc_agent.state.queue_intake_chain import record_chain_intake

    first = record_chain_intake(tmp_path, run_id=_RUN_ID, origin_block="submit-s1")
    assert first is not None and first["recorded"] is True
    second = record_chain_intake(tmp_path, run_id=_RUN_ID, origin_block="submit-s1")
    assert second is not None and second["recorded"] is False
    items = read_intake_items(tmp_path)
    assert len([i for i in items if i.get("run_id") == _RUN_ID]) == 1


def test_chain_queue_intake_no_ops_without_a_run(tmp_path: Path) -> None:
    """A boundary that minted no run records nothing — and says nothing."""
    from hpc_agent.state.queue_intake_chain import record_chain_intake

    assert record_chain_intake(tmp_path, run_id=None, origin_block="submit-s1") is None


# ── 6. the ONE-``y`` end-to-end ───────────────────────────────────────────────


@pytest.fixture
def experiment_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "_home"))
    clusters = tmp_path / "clusters.yaml"
    clusters.write_text(f"{_CLUSTER}:\n  host: {_CLUSTER}.example.edu\n", encoding="utf-8")
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(clusters))
    exp = tmp_path / "exp"
    exp.mkdir()
    return exp


def _arm_wake() -> None:
    lease = overnight._watch_lease_path(_RUN_ID)
    lease.parent.mkdir(parents=True, exist_ok=True)
    lease.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")


def _sidecar(experiment_dir: Path) -> None:
    write_run_sidecar(
        experiment_dir,
        run_id=_RUN_ID,
        cmd_sha=_CMD_SHA,
        hpc_agent_version="0.2.0",
        submitted_at="2026-07-30T00:00:00+00:00",
        executor="python3 widget.py",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=4,
        tasks_py_sha="1" * 64,
        cluster=_CLUSTER,
        resources={"walltime_sec": 600, "cpus": 2},
    )


def _record_consent(experiment_dir: Path, *, response: str, resolved: dict[str, Any]) -> Any:
    """Record the standing consent through the REAL append_decision gate."""
    return append_decision(
        experiment_dir=experiment_dir,
        spec=AppendDecisionInput.model_validate(
            {
                "scope_kind": "run",
                "scope_id": _RUN_ID,
                "block": overnight.OVERNIGHT_CONSENT_BLOCK,
                "response": response,
                "resolved": resolved,
            }
        ),
    )


def _gate(experiment_dir: Path, verb: str, predecessor: str) -> Any:
    """One consent-aware gate, called exactly as its block calls it."""
    return assert_greenlit_or_consented(
        experiment_dir,
        run_id=_RUN_ID,
        verb=verb,
        predecessor=predecessor,
        current_cmd_sha=read_run_cmd_sha(experiment_dir, _RUN_ID),
        current_placement=read_run_cluster(experiment_dir, _RUN_ID),
        clean_predecessor=True,
    )


def test_one_typed_grant_covers_the_whole_submit_chain(experiment_dir: Path) -> None:
    """R-b's census, mechanized: NO gate was removed, and one grant carries the
    CONSUMABLE boundaries — which is NOT all four.

    The shrink R-b asks for is CONSENT-MODE behaviour, not a smaller gate set, and
    this is the test that says so out loud. Every gate in ``GATED_BLOCKS`` is still
    there and still consulted. What a single typed standing grant — the ONE human
    act, offered at the run's FIRST rendezvous under R-a — carries is exactly the
    set ``overnight.is_consumable_boundary`` declares consumable, and the
    assertions below read that set from the code rather than naming it.

    **What it does NOT authorize, stated because overclaiming here would be the
    worst kind of documentation error:** ``submit-s2`` still demands its own
    greenlight even under a live grant. S2 is the run's FIRST spend — the boundary
    where local intent becomes remote reality — and the substrate deliberately
    withholds it from the consumable set. The second loop below asserts that
    refusal, so "one typed act" is a claim about the boundaries the substrate
    names and never a claim about the whole chain.

    Division of labour with the censuses: if a future change DELETES a gate to
    make a chain flow, the membership censuses turn red
    (``tests/ops/test_block_chain_coverage.py`` and
    ``tests/integration/test_spec_contract.py``'s gated/ungated partition); this
    test owns the separate claim that the census does not need shrinking.
    """
    _arm_wake()
    _sidecar(experiment_dir)
    # THE ONE HUMAN ACT — the grant line rendered by the SAME one-home renderer
    # the park-time offer uses (``render_grant_line``), so what the test types is
    # byte-for-byte what R-a offers at the S1 rendezvous. ``expires_at`` is pinned
    # here only because the test must type the line BEFORE the poka-yoke composes
    # its own default; the offer at a real park renders the composed value.
    expires_at = "2026-12-31T15:00:00+00:00"
    resolved = {
        "budget_cap": 500.0,
        "cmd_sha": _CMD_SHA,
        "expires_at": expires_at,
        "placement": {_CLUSTER: {"budget_cap": 500.0}},
    }
    # The gate REFUSES first, and its refusal renders the paste-ready line (the
    # ONE grant-vocabulary home — offer and refusal cannot drift). Reading the
    # line off the refusal is the ``test_consent_lifecycle_journey`` idiom, and it
    # is what makes this test a real proof rather than a re-typing of the tokens.
    with pytest.raises(errors.SpecInvalid) as refusal:
        _record_consent(experiment_dir, response="y", resolved=resolved)
    grant_line = [ln.strip() for ln in str(refusal.value).splitlines() if ln.strip()][-1]
    assert grant_line == overnight.render_grant_line(
        scope_kind="run",
        scope_id=_RUN_ID,
        cmd_sha=_CMD_SHA,
        placement=(_CLUSTER,),
        expires_at=expires_at,
    )
    # The AUTHORSHIP leg: the human TYPED it, so it is in the utterance log. That
    # is the whole trust story of R-a — offering the grant one rendezvous earlier
    # moves WHEN the human types it, never WHETHER they do.
    append_utterance(experiment_dir, grant_line)
    _record_consent(experiment_dir, response=grant_line, resolved=resolved)

    # The gate census is UNCHANGED — all four are still gated…
    assert set(block_chain.GATED_BLOCKS) == {
        "submit-s2",
        "submit-s3",
        "submit-s4",
        "aggregate-run",
    }
    # …and every boundary the consent substrate declares CONSUMABLE clears under
    # that single grant, with no further human act. The consumable set is READ
    # from the code (`overnight.is_consumable_boundary`), never re-asserted here:
    # this test's claim is "one grant carries the chain", and a hand-copied set
    # would quietly weaken it to "one grant carries the boundaries I remembered".
    predecessors = {
        "submit-s2": "S1",
        "submit-s3": "S2",
        "submit-s4": "S3",
        "aggregate-run": "aggregate-check",
    }
    consumable = [
        v
        for v in predecessors
        # ``clean_predecessor=True`` mirrors what ``_gate`` passes below: the
        # tier-3 boundaries are consumable only off a clean terminal, and asking
        # the predicate under different evidence than the gate sees would make
        # the two disagree about the same run.
        if overnight.is_consumable_boundary("run", v, clean_predecessor=True)
    ]
    assert consumable, "no gated boundary is consumable - the grant would carry nothing"
    for verb in consumable:
        _gate(experiment_dir, verb, predecessors[verb])

    # THE FLOOR, stated rather than assumed: a boundary the substrate does NOT
    # declare consumable still demands its own greenlight even under a live
    # standing consent. R-b shrinks consent-MODE behaviour; it does not dissolve
    # the trust floor, and this pins that the floor is still load-bearing.
    for verb in predecessors:
        if verb in consumable:
            continue
        with pytest.raises(errors.SpecInvalid):
            _gate(experiment_dir, verb, predecessors[verb])

    # Exactly ONE human decision record exists for the whole run.
    from hpc_agent.state.decision_journal import read_decisions

    human_acts = [
        rec
        for rec in read_decisions(experiment_dir, "run", _RUN_ID)
        if rec.get("block") == overnight.OVERNIGHT_CONSENT_BLOCK
    ]
    assert len(human_acts) == 1


def test_the_grant_offer_discloses_its_full_spend_envelope(experiment_dir: Path) -> None:
    """R-a's D1 bar: a grant offered at the START must say what it authorizes."""
    _sidecar(experiment_dir)
    envelope = overnight.compose_spend_envelope(experiment_dir, _RUN_ID)
    assert envelope is not None
    assert envelope["task_count"] == 4
    assert envelope["walltime_sec"] == 600
    assert envelope["cpus"] == 2
    assert envelope["requested_wall_seconds"] == 2400
    assert envelope["est_core_hours"] is not None
    assert envelope["cluster"] == _CLUSTER


def test_the_spend_envelope_is_absent_never_fabricated(experiment_dir: Path) -> None:
    """No sidecar → no envelope. A made-up spend figure beside a consent request
    would be the worst possible disclosure, so ``None`` is the contract."""
    assert overnight.compose_spend_envelope(experiment_dir, _RUN_ID) is None
