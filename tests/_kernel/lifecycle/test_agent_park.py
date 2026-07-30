"""The AGENT-ACTOR park carries ZERO consent semantics (prelude P2.a).

``block-drive``'s park machinery was built entirely around a HUMAN typing
``y``/paste/nudge at a rendezvous, and every affordance it composes there is a
CONSENT affordance: the greenlight target, the scoped-consent ``approve_hint``,
the overnight standing-consent grant offer, the answer menu's bare-``y`` line.
P2.a adds a second actor — a park that routes to the LLM with a DRAFT brief —
and the load-bearing claim is that **a draft is authorship, not authorization**.

That claim is only worth anything if it cannot be satisfied by accident, so this
file attacks it from both directions:

* the COMPOSITION side — an agent park must resolve no target and must emit none
  of the four consent affordances (and the ``draft_ask`` it emits instead must
  say what is owed);
* the CONSUMPTION side — the mutation-style leg: an ``awaiting_draft`` park must
  never satisfy ``ops/block_gate.assert_greenlit_or_consented``, and the driver's
  resume must advance it WITHOUT reading a committed greenlight — even when a
  perfectly-targeted greenlight for that very boundary is sitting in the journal.

Plus the regression floor the whole change rides on: a HUMAN park's marker is
BYTE-IDENTICAL to its pre-P2.a self.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

from hpc_agent import errors
from hpc_agent._kernel.lifecycle import block_drive
from hpc_agent._kernel.lifecycle.answer_menu import answer_menu_of, compose_draft_ask, draft_ask_of
from hpc_agent._kernel.lifecycle.block_drive import greenlight_target, park, run_tick
from hpc_agent.infra import block_chain
from hpc_agent.state.journal import read_pending_decision

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "audit_abcd1234"
_AGENT_VERB, _AGENT_STAGE = ("audit-preflight", "awaiting_draft")


@pytest.fixture(autouse=True)
def _isolated_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A per-test journal home + a no-op watchdog stamp (the block-drive idiom)."""
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    import hpc_agent._kernel.lifecycle.drive as drive_mod

    monkeypatch.setattr(drive_mod, "_stamp_driver_tick", lambda *_a, **_k: None)


def _mint_record(experiment_dir: Path, run_id: str = _RUN_ID) -> None:
    """A journal RunRecord so ``mark_pending_decision`` can persist the marker."""
    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord

    upsert_run(
        experiment_dir,
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
            experiment_dir=str(experiment_dir),
            status="in_flight",
        ),
    )


def _park_agent(experiment_dir: Path, *, spec: dict[str, Any] | None = None) -> dict[str, Any]:
    """Park the agent boundary and return the driver's copy of the result brief."""
    result: dict[str, Any] = {
        "stage_reached": _AGENT_STAGE,
        "needs_decision": True,
        "brief": {"verdict": "GO", "source_present": False},
    }
    park(
        experiment_dir,
        run_id=_RUN_ID,
        workflow="audit",
        verb=_AGENT_VERB,
        stage=_AGENT_STAGE,
        successor=None,
        spec=spec or {"audit_id": _RUN_ID, "source": "s.py", "template": "t.py"},
        result=result,
    )
    return result


# ── the registry itself ───────────────────────────────────────────────────────


def test_agent_parks_registry_is_explicit_and_narrow() -> None:
    """Membership is a deliberate edit, never a stage-name convention.

    If ``park_actor`` ever derived the actor from a spelling (``*_draft``), a
    future terminator could become an agent park — and lose every consent
    affordance — by being NAMED a certain way. Pin that it is a lookup.
    """
    assert (_AGENT_VERB, _AGENT_STAGE) in block_chain.AGENT_PARKS
    assert block_chain.park_actor(_AGENT_VERB, _AGENT_STAGE) == "agent"
    # A same-verb sibling stage is NOT an agent park, so the key is the pair.
    assert block_chain.park_actor(_AGENT_VERB, "preflight_blocked") == "human"
    # A convincingly-named stranger is not one either.
    assert block_chain.park_actor("made-up-block", "awaiting_draft") == "human"
    for verb, stage in sorted(block_chain.SUCCESSORS):
        if (verb, stage) not in block_chain.AGENT_PARKS:
            assert block_chain.park_actor(verb, stage) == "human", (verb, stage)


# ── composition: no consent affordance exists at an agent park ────────────────


def test_agent_park_resolves_no_greenlight_target() -> None:
    """Leg 1: nothing for a ``y`` to greenlight — by construction, not omission.

    ``greenlight_target`` falls back to the block's chain-forward successor when
    the parked successor is None, and ``audit-preflight`` HAS one
    (``notebook-lint``). So without the actor rule this boundary would silently
    offer a bare-``y`` advance into the lint — consent minted from a drafting
    ask. Assert the fallback is what the rule overrides.
    """
    assert block_chain.chain_successor(_AGENT_VERB) == "notebook-lint"
    assert greenlight_target(_AGENT_VERB, None, stage=_AGENT_STAGE) is None
    # The same call WITHOUT the stage keeps the pre-P2.a human derivation, so
    # every existing call site is byte-identical.
    assert greenlight_target(_AGENT_VERB, None) == "notebook-lint"
    # And a human stage on the same verb still resolves normally.
    assert greenlight_target(_AGENT_VERB, None, stage="preflight_blocked") == "notebook-lint"


def test_agent_park_composes_no_consent_affordances(tmp_path: Path) -> None:
    """Leg 2: none of the four consent affordances rides an agent park's brief."""
    _mint_record(tmp_path)
    result = _park_agent(tmp_path)
    brief = result["brief"]
    for consent_key in ("approve_hint", "answer_menu", "standing_offer"):
        assert consent_key not in brief, f"agent park composed {consent_key}"
    assert answer_menu_of(brief) is None
    marker = read_pending_decision(_RUN_ID, experiment_dir=tmp_path)
    for consent_key in ("approve_hint", "answer_menu", "standing_offer"):
        assert consent_key not in marker["brief"], f"agent park PERSISTED {consent_key}"


def test_agent_park_emits_a_draft_ask_that_states_what_is_owed(tmp_path: Path) -> None:
    """The replacement for the answer menu says what the AGENT must produce.

    A park that composed nothing at all would be a worse outcome than one that
    offers a wrong ``y``: the LLM would be stopped with no stated ask. The
    ``draft_ask`` is the code-authored substitute, and it must be honest about
    granting nothing.
    """
    _mint_record(tmp_path)
    ask = draft_ask_of(_park_agent(tmp_path)["brief"])
    assert ask is not None
    assert ask["actor"] == "agent"
    assert ask["kind"] == "awaiting_draft"
    assert "bare_y_ok" not in ask and "answer_line" not in ask
    assert ask["ask"].strip()
    text = ask["text"].lower()
    assert "authorship" in text and "authorization" in text


def test_draft_ask_renders_nothing_for_an_unregistered_boundary() -> None:
    """The composer never INVENTS an instruction for a boundary it has no ask for."""
    assert compose_draft_ask(block="made-up-block", stage="made_up", run_id="r1") is None


def test_human_park_marker_is_byte_identical(tmp_path: Path) -> None:
    """The regression floor: a HUMAN park's persisted marker did not move.

    The ``actor`` key is written ONLY on the agent branch, so a human marker
    carries no trace of P2.a — a reader that knows nothing about actors keeps
    reading exactly what it read before.
    """
    _mint_record(tmp_path)
    result: dict[str, Any] = {
        "stage_reached": "not_ready",
        "needs_decision": True,
        "brief": {"why": "human"},
    }
    park(
        tmp_path,
        run_id=_RUN_ID,
        workflow="aggregate",
        verb="aggregate-check",
        stage="not_ready",
        successor=None,
        spec={"run_id": _RUN_ID},
        result=result,
    )
    marker = read_pending_decision(_RUN_ID, experiment_dir=tmp_path)
    assert "actor" not in marker
    assert set(marker) == {
        "block",
        "workflow",
        "brief",
        "resume_cursor",
        "awaiting_since",
        "cmd_sha",
    }
    assert block_drive._pending_park_actor(marker) == "human"
    # The human park still gets its consent affordances — the agent branch did
    # not accidentally disarm them for everyone.
    assert answer_menu_of(marker["brief"]) is not None


def test_agent_park_marker_records_the_actor(tmp_path: Path) -> None:
    _mint_record(tmp_path)
    _park_agent(tmp_path)
    marker = read_pending_decision(_RUN_ID, experiment_dir=tmp_path)
    assert marker["actor"] == "agent"
    assert block_drive._pending_park_actor(marker) == "agent"


# ── consumption: the mutation-style legs ──────────────────────────────────────


def test_agent_park_never_satisfies_assert_greenlit_or_consented(tmp_path: Path) -> None:
    """THE mutation-style leg: parking for a draft authorizes nothing, anywhere.

    A drafting park is a stop, and a stop that could be mistaken for an approval
    is the fabricated-approval class. Assert the consent-aware gate — the one
    every cluster-spending block calls — still REFUSES after the agent park was
    written, exactly as it does with no park at all: the park journals no
    decision record, so there is nothing for either gate leg to find.
    """
    from hpc_agent.ops.block_gate import assert_greenlit_or_consented

    _mint_record(tmp_path)
    _park_agent(tmp_path)
    with pytest.raises(errors.SpecInvalid):
        assert_greenlit_or_consented(
            tmp_path,
            run_id=_RUN_ID,
            verb="notebook-lint",
            predecessor=_AGENT_VERB,
            current_cmd_sha="0" * 64,
        )
    # Nothing was minted into the decision journal by the park itself.
    from hpc_agent.state.decision_journal import read_decisions

    assert read_decisions(tmp_path, "run", _RUN_ID) == []


def _agent_park_then_tick(
    tmp_path: Path, *, spans: list[tuple[str, dict[str, Any]]]
) -> tuple[Any, list[str]]:
    """Park the agent boundary, then run ONE tick with the block spans faked."""
    seen: list[str] = []

    def _fake_span(verb: str, spec: dict[str, Any], experiment_dir: Path) -> tuple[dict, int]:
        seen.append(verb)
        return (dict(spans.pop(0)[1]) if spans else {}), 0

    with mock.patch.object(block_drive, "_run_block_verb", side_effect=_fake_span):
        result, _code = run_tick(tmp_path, run_id=_RUN_ID, workflow="audit")
    return result, seen


def test_agent_park_resume_consumes_no_committed_greenlight(tmp_path: Path) -> None:
    """The resume RE-RUNS the parked block — it never asks the journal anything.

    With NO greenlight journaled at all, a human park would report
    ``awaiting_decision`` and run nothing. The agent park instead re-runs
    ``audit-preflight`` so the block re-reads the world: the draft is EVIDENCE ON
    DISK, and that is the only thing that can discharge the ask.
    """
    _mint_record(tmp_path)
    _park_agent(tmp_path)
    result, seen = _agent_park_then_tick(
        tmp_path,
        spans=[
            (
                _AGENT_VERB,
                {
                    "stage_reached": "preflight_go",
                    "needs_decision": False,
                    "next_block": {"verb": "notebook-lint", "why": "go", "spec_hint": {}},
                },
            ),
            ("notebook-lint", {"stage_reached": "linted", "needs_decision": False}),
        ],
    )
    assert seen == [_AGENT_VERB, "notebook-lint"]
    assert result.action == "terminal"
    # The marker was consumed by the re-run, not left parked forever.
    assert read_pending_decision(_RUN_ID, experiment_dir=tmp_path) == {}


def test_agent_park_resume_ignores_a_greenlight_committed_for_the_boundary(
    tmp_path: Path,
) -> None:
    """The sharp case: a PERFECTLY targeted ``y`` must change nothing.

    A greenlight naming this boundary's chain-forward block is exactly what would
    advance a HUMAN park here. If the agent park routed through the ordinary
    resume, that record would consume the park and chain into ``notebook-lint``
    with no draft written — consent laundered into authorship. The agent leg
    re-runs the parked block regardless, so the journaled ``y`` is inert: the
    first span is ``audit-preflight``, never the successor.
    """
    from hpc_agent.state.decision_journal import append_decision

    _mint_record(tmp_path)
    _park_agent(tmp_path)
    append_decision(
        tmp_path,
        scope_kind="run",
        scope_id=_RUN_ID,
        block=_AGENT_VERB,
        response="y",
        resolved={"next_block": "notebook-lint"},
    )
    _result, seen = _agent_park_then_tick(
        tmp_path,
        spans=[
            (
                _AGENT_VERB,
                {"stage_reached": _AGENT_STAGE, "needs_decision": True, "brief": {}},
            )
        ],
    )
    assert seen == [_AGENT_VERB], "a committed greenlight advanced an agent park"
    # Re-parked identically: the draft still is not there, so the ask stands.
    marker = read_pending_decision(_RUN_ID, experiment_dir=tmp_path)
    assert marker["block"] == _AGENT_VERB
    assert marker["actor"] == "agent"


def test_agent_park_resume_is_idempotent_when_the_draft_never_lands(tmp_path: Path) -> None:
    """No draft ⇒ the same park, tick after tick. One span per tick, never a spin."""
    _mint_record(tmp_path)
    _park_agent(tmp_path)
    for _ in range(3):
        _result, seen = _agent_park_then_tick(
            tmp_path,
            spans=[(_AGENT_VERB, {"stage_reached": _AGENT_STAGE, "needs_decision": True})],
        )
        assert seen == [_AGENT_VERB]
        assert read_pending_decision(_RUN_ID, experiment_dir=tmp_path)["actor"] == "agent"


def test_agent_park_resume_reparks_on_a_failed_span(tmp_path: Path) -> None:
    """A failed re-run must not lose the park (the F14 re-park guard, agent leg).

    The marker is consumed by a compare-and-swap BEFORE the span runs, so a span
    that fails would otherwise drop the ask on the floor and leave the loop with
    no record of what it was waiting for.
    """
    _mint_record(tmp_path)
    _park_agent(tmp_path)
    with mock.patch.object(block_drive, "_run_block_verb", return_value=({}, 1)):
        result, code = run_tick(tmp_path, run_id=_RUN_ID, workflow="audit")
    assert result.action == "skip"
    assert code != 0
    marker = read_pending_decision(_RUN_ID, experiment_dir=tmp_path)
    assert marker["block"] == _AGENT_VERB
    assert marker["actor"] == "agent"


def test_agent_park_marker_survives_a_json_round_trip(tmp_path: Path) -> None:
    """The marker is read back off disk, so the actor must survive serialization."""
    _mint_record(tmp_path)
    _park_agent(tmp_path)
    marker = read_pending_decision(_RUN_ID, experiment_dir=tmp_path)
    assert block_drive._pending_park_actor(json.loads(json.dumps(marker))) == "agent"
    # A marker with a torn/absent actor degrades to the human path, never crashes.
    assert block_drive._pending_park_actor({"block": "x"}) == "human"
    assert block_drive._pending_park_actor({"block": "x", "actor": ""}) == "human"
