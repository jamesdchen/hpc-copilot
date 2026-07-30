"""Bare-``y`` coverage census — every park boundary either takes one keystroke or says why.

THE RULE (block-drive R3 consumption leg): a plain ``y`` at a parked boundary
advances under the MATERIALIZED successor spec. That is what makes the human
side of this fork cheap — the decision is a judgment, not a transcription
exercise. A boundary that quietly lacks a materialized default breaks the rule
INVISIBLY: nothing raises, the human just types ``y`` and nothing happens (the
run-13 ``causal_tune_linear-de448128`` wedge was exactly this class, and it took
a live run to find). Prose cannot catch that; this census can.

The rule, mechanized: every ``(block_verb, stage_reached)`` pair that can PARK
for a human decision must either

* (a) resolve to a greenlight TARGET under the one shared rule the driver and the
  Stop guard both apply (:func:`hpc_agent._kernel.lifecycle.block_drive.greenlight_target`
  — the parked successor, else the block's chain-forward successor), so a bare
  ``y`` has something to advance to and
  :func:`hpc_agent._kernel.lifecycle.answer_menu.compose_answer_menu` offers it as
  the menu's first line; **or**
* (b) sit on :data:`ALLOWLIST` with a stated reason why agreement cannot be one
  keystroke there.

Adding a park boundary without deciding the question fails this test — which is
the point: the decision must be made, in either direction, on purpose.

CENSUS CONSTRUCTION (registry + AST, so it cannot go blind):

* the boundary universe is :data:`block_chain.SUCCESSORS` — the SoT for
  ``(verb, stage) -> successor``;
* a pair is a PARK when the driver stops there (``_kernel/lifecycle/block_drive._chain``):
  the block set ``needs_decision``, or its successor is greenlight-GATED
  (:func:`block_chain.is_gated`) so the in-code chain must stop for the human;
* "can set ``needs_decision``" is read from the SOURCE (:func:`_decision_stages`):
  every ``stage_reached=<literal>`` that appears in a call alongside a
  ``needs_decision`` that is not literally ``False``. Conservative on purpose — a
  computed ``needs_decision`` counts as "can be True", because a census that
  under-counts is a census that silently stops guarding.

Follows the caller-census pattern of ``tests/contracts/test_placement_leg_callers.py``,
including its guard-the-guard tests: the census must still see the boundaries known
today, the allowlist must hold no stale entry, and the audit must be shown to FAIL
on a synthetic uncovered boundary.
"""

from __future__ import annotations

import ast
from pathlib import Path

import hpc_agent
from hpc_agent._kernel.lifecycle.answer_menu import compose_answer_menu
from hpc_agent._kernel.lifecycle.block_drive import greenlight_target
from hpc_agent.infra import block_chain

SRC_ROOT = Path(hpc_agent.__file__).resolve().parent

#: (block_verb, stage_reached) -> why agreement at this boundary cannot be one
#: keystroke. Every entry must name a REAL reason; "forgot" is not one, and
#: "the target is None" is a restatement, not a reason — say what the human is
#: being asked for instead.
ALLOWLIST: dict[tuple[str, str], str] = {
    ("submit-s4", "harvested"): (
        "end of the submit chain. The brief hands over an EMPTY "
        "``proposed_interpretations`` list (§2 — concluding is the human's), so the "
        "answer is an INTERPRETATION the human authors over the results table, not "
        "agreement to a next block. There is no successor a `y` could name."
    ),
    ("submit-s4", "harvest_partial"): (
        "as ``harvested``, plus escalated waves the human must weigh before "
        "interpreting a partial table. Still the end of the chain — no successor."
    ),
    ("aggregate-run", "harvested"): (
        "end of the aggregate chain — the same interpretation decision as "
        "``submit-s4 harvested``, reached by the standalone aggregate route."
    ),
    ("aggregate-run", "harvest_partial"): (
        "end of the aggregate chain, partial table — see ``submit-s4 harvest_partial``."
    ),
    ("status-watch", "watch_anomaly"): (
        "a §5 anomaly evidence brief at the LAST block of the status family. Recovery "
        "(resubmit-failed / kill / reconcile) is a genuine human branch with no single "
        "deterministic successor — ``block_chain.recovery_arm_verb`` states it outright "
        '("a bare `y` at an anomaly has no successor"), and the DELTA the human names '
        "is what selects the arm. The brief's structured recommendation still rides the "
        "answer menu as a paste-line, so the answer is one paste even without a `y`."
    ),
    ("campaign-complete", "complete"): (
        "terminal of the campaign chain: the human's answer is the campaign's "
        "CONCLUSION, which is a value only they can supply. No successor exists."
    ),
    # ── campaign-refill (surfaced 2026-07-30 by the indirection leg) ──────────
    # These three were INVISIBLE to the census until ``_literal_bindings`` landed:
    # ``ops/campaign_refill.py`` picks its terminator into a local ``stage`` and
    # passes the NAME, so the constants-only walk saw nothing and the whole
    # single-member family went unguarded. ``refill_blocked`` is a genuine park
    # (``needs_decision`` is true whenever a blocked slot is not merely
    # claim-held/pool-full); the other two are decision-free terminals the coarse
    # name-resolution necessarily sweeps in with it. All three are stated rather
    # than silently dropped — that is the ledger doing its job on a boundary
    # nobody had decided about.
    ("campaign-refill", "refill_blocked"): (
        "a side-spur terminal with no chain-forward successor (``campaign-refill`` is "
        "its own single-member family, deliberately OUT of the campaign chain so it "
        "cannot shift the three real touchpoints' block_index positions). What the "
        "human must supply is the RESOLUTION the blocked slot named — resume-vs-fresh, "
        "the scaffold interview, or whatever the gate refused on — not agreement to a "
        "next block. The next tick re-enters through campaign-watch."
    ),
    ("campaign-refill", "refilled"): (
        "not a question at all: the tick dispatched its slots and the chain ENDS (the "
        "next cron/loop tick re-enters via campaign-watch, one step per tick). "
        "``needs_decision`` is false on this arm; it is visible to the census only "
        "because the block picks its terminator into a local the coarse "
        "name-resolution cannot narrow. Nothing is being asked, so nothing is owed."
    ),
    ("campaign-refill", "no_refill_needed"): (
        "as ``refilled``: advance said wait/stop/continue, so the tick is a disclosed "
        "no-op and the chain ends with nothing asked of anyone. Visible to the census "
        "only through the same shared-local terminator; ``needs_decision`` is false."
    ),
    ("audit-preflight", "awaiting_draft"): (
        "an AGENT park (``block_chain.AGENT_PARKS``), and therefore the one boundary "
        "class a bare ``y`` structurally cannot apply to: what is owed here is a "
        "DRAFT — the LLM writes or revises the audited source ``.py`` — and a draft "
        "is AUTHORSHIP, not authorization. There is nothing to greenlight, so "
        "``greenlight_target`` returns None by construction rather than by omission, "
        "and the park composes a ``draft_ask`` in place of an answer menu. The "
        "evidence that the ask was met is the FILE ON DISK, which the next tick "
        "re-reads; no journaled approval is sought and none is ever consumed."
    ),
    ("wrap-entry-point-auto", "needs_wrapper_argv"): (
        "the onboard chain's second AGENT park (``block_chain.AGENT_PARKS``, P2.c). "
        "``detect-entry-point`` already read the CLI parameters mechanically off the "
        "AST and carried them through on ``argv_params``; what is owed is their "
        "TRANSCRIPTION into an argv template + typed signature, which is AUTHORSHIP, "
        "not authorization. Nothing is greenlit and nothing is spent, so "
        "``greenlight_target`` returns None by construction — a bare ``y`` here "
        "would advance into ``interview`` under a wrapper that still has no argv, "
        "and every task would fail on the missing flags. The sibling stage "
        "``needs_wrapper_argv_unsupported`` is a HUMAN park with a chain-forward "
        "override target, and it is a SEPARATE stage precisely so this one can be "
        "registered as the agent's without a runtime sniff."
    ),
    ("notebook-status", "sections_pending"): (
        "the audit's SIGN-OFF rendezvous, and the whole point of the T8 tier is that "
        "agreement here is NOT one keystroke: the human types a ``notebook-sign-off`` "
        "through ``append-decision``, binding the section shas and the ``view_sha`` "
        "they actually read (rarity-buys-seriousness — palatability must never reach "
        "the effortful tier). It is also the last block of the audit chain, so no "
        "chain-forward successor exists for a ``y`` to override into."
    ),
}


def _literal_bindings(tree: ast.AST) -> dict[str, set[str]]:
    """Every ``name -> {string literals}`` binding assigned anywhere in *tree*.

    The indirection leg of the scan below. A terminator that picks its stage into
    a LOCAL first — ``stage_reached: Literal[...] = "a" if cond else "b"``, then
    ``Result(stage_reached=stage_reached, ...)`` — hands the call an ``ast.Name``
    with no constants inside it, so a constants-only walk of the call site
    reports NOTHING and the census silently stops guarding that boundary (the
    audit chain's ``audit-preflight`` / ``notebook-status`` terminators are
    written exactly that way). Resolving the name against the file's assignments
    is deliberately COARSE — module-wide, no scope analysis, unioning every
    binding of that name — because over-reporting a stage costs at most a
    spurious "give this boundary a target or a reason", while under-reporting
    costs an unguarded park.
    """
    bindings: dict[str, set[str]] = {}

    def _record(target: ast.expr, value: ast.expr | None) -> None:
        if not isinstance(target, ast.Name) or value is None:
            return
        literals = {
            const.value
            for const in ast.walk(value)
            if isinstance(const, ast.Constant) and isinstance(const.value, str)
        }
        if literals:
            bindings.setdefault(target.id, set()).update(literals)

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                _record(target, node.value)
        elif isinstance(node, ast.AnnAssign):
            _record(node.target, node.value)
    return bindings


def _decision_stages() -> set[str]:
    """Every ``stage_reached`` literal that can ship with ``needs_decision`` true.

    AST over ``src/``: any call carrying BOTH keywords, keeping every string
    constant inside the ``stage_reached`` expression (a terminator may pick its
    stage with a conditional — ``"harvest_partial" if partial else "harvested"``
    — and both arms are real boundaries), plus — via :func:`_literal_bindings` —
    the literals a NAMED stage expression was bound to in the same file. A
    ``needs_decision`` that is literally ``False`` is excluded; anything else
    (``True``, or a computed expression) is counted, because the census must
    never under-report.
    """
    stages: set[str] = set()
    for path in sorted(SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        bindings = _literal_bindings(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            stage_node = kwargs.get("stage_reached")
            decision_node = kwargs.get("needs_decision")
            if stage_node is None or decision_node is None:
                continue
            if isinstance(decision_node, ast.Constant) and decision_node.value is not True:
                continue
            for sub in ast.walk(stage_node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    stages.add(sub.value)
                elif isinstance(sub, ast.Name):
                    stages |= bindings.get(sub.id, set())
    return stages


def _park_boundaries() -> dict[tuple[str, str], str | None]:
    """Every ``(verb, stage)`` the driver can PARK at -> its greenlight target.

    A pair parks when the block can set ``needs_decision`` at that stage, or when
    its successor is greenlight-gated (the in-code chain cannot journal the ``y``
    the gate needs, so it stops). ``detached`` stages are excluded: the driver
    returns the detached handle there and the brief arrives via the journal — no
    park, no decision to answer.
    """
    decision_stages = _decision_stages()
    boundaries: dict[tuple[str, str], str | None] = {}
    for (verb, stage), successor in block_chain.SUCCESSORS.items():
        if stage == "detached":
            continue
        gated = successor is not None and block_chain.is_gated(successor)
        if stage not in decision_stages and not gated:
            continue
        # ``stage`` is passed so an AGENT park (P2.a) resolves ``None`` — a draft
        # is authorship, not authorization, so no ``y`` at that boundary
        # greenlights anything and the census must demand a stated reason.
        boundaries[(verb, stage)] = greenlight_target(verb, successor, stage=stage)
    return boundaries


def _uncovered(
    boundaries: dict[tuple[str, str], str | None], allowlist: dict[tuple[str, str], str]
) -> list[tuple[str, str]]:
    """The audit itself, as a pure function so it can be exercised on a fixture.

    A boundary is uncovered when it has no greenlight target (no materialized
    default for a bare ``y``) and no allowlist entry stating why.
    """
    return [
        key for key, target in sorted(boundaries.items()) if not target and key not in allowlist
    ]


# ── the audit ─────────────────────────────────────────────────────────────────


def test_every_park_boundary_takes_a_bare_y_or_states_why_not() -> None:
    uncovered = _uncovered(_park_boundaries(), ALLOWLIST)
    assert not uncovered, (
        "Park boundary/-ies that neither accept a bare `y` nor sit on the allowlist "
        "with a stated reason. A human who types `y` there gets silence: the shared "
        "boundary rule resolves no target, so nothing advances (the run-13 wedge "
        "class). Give the boundary a chain-forward successor, or add an ALLOWLIST "
        f"entry naming what the human must supply instead: {uncovered}"
    )


def test_bare_y_boundaries_all_render_a_y_line_in_their_answer_menu() -> None:
    """Leg (a) is only real if the human can SEE it: every accepting boundary's menu
    leads with the pasteable ``y`` and names the target it advances to."""
    for (verb, stage), target in sorted(_park_boundaries().items()):
        if not target:
            continue
        menu = compose_answer_menu(brief={}, block=verb, run_id="r1", target=target)
        assert menu is not None, f"{(verb, stage)} has target {target} but renders no menu"
        assert menu["bare_y_ok"] is True, f"{(verb, stage)} does not advertise a bare y"
        assert menu["options"][0]["paste"] == "y"
        assert target in menu["options"][0]["means"]
        assert menu["answer_line"] == "y"


def test_allowlisted_boundaries_still_offer_a_pasteable_answer_when_data_allows() -> None:
    """The allowlist buys an exemption from the bare ``y``, NOT from the menu: a
    boundary carrying structured recommendation data still renders its paste-lines,
    so "no one-keystroke answer" never means "reconstruct the grammar yourself"."""
    brief = {"recommendation": {"action": "classify-failed-tasks", "then": "resubmit-failed"}}
    for verb, stage in sorted(ALLOWLIST):
        menu = compose_answer_menu(brief=brief, block=verb, run_id="r1", target=None)
        assert menu is not None, f"{(verb, stage)} renders no menu even with recommendation data"
        assert menu["bare_y_ok"] is False
        assert menu["options"][0]["paste"] == "classify-failed-tasks, then resubmit-failed"


# ── guard-the-guard ───────────────────────────────────────────────────────────


def test_census_still_sees_the_known_park_boundaries() -> None:
    """The census must not go blind: if a refactor renames the stage literals or
    moves the terminators, an empty census would make the audit pass vacuously.
    Pin one boundary per workflow family (accepting and allowlisted alike)."""
    seen = _park_boundaries()
    for expected in [
        ("submit-s1", "needs_resolution"),
        ("submit-s2", "canary_failed"),
        ("submit-s3", "watching_anomaly"),
        ("submit-s4", "harvested"),
        ("status-snapshot", "snapshot_anomaly"),
        ("status-watch", "watch_anomaly"),
        ("aggregate-check", "not_ready"),
        ("aggregate-run", "harvested"),
        ("campaign-greenlight", "needs_greenlight"),
        ("campaign-complete", "complete"),
    ]:
        assert expected in seen, f"census no longer finds {expected} — did a refactor move it?"


def test_census_finds_the_gated_parks_even_though_they_are_clean_terminators() -> None:
    """``submit-s1 resolved`` / ``aggregate-check ready`` are CLEAN stages, but their
    successor is greenlight-gated so the driver parks anyway. Dropping the
    ``is_gated`` leg would silently shrink the universe the audit walks."""
    seen = _park_boundaries()
    for expected in [("submit-s1", "resolved"), ("aggregate-check", "ready")]:
        assert expected in seen
        assert seen[expected] == block_chain.SUCCESSORS[expected]


def test_detached_stages_are_not_park_boundaries() -> None:
    """A detached handle is not a decision — counting it would demand a bare-y
    default for a boundary that never asks the human anything."""
    assert not [key for key in _park_boundaries() if key[1] == "detached"]


def test_decision_stage_scan_is_not_empty_and_reads_conditional_stages() -> None:
    """The AST leg's own guard: it must find stages, and it must see BOTH arms of a
    conditional ``stage_reached`` (``submit-s4`` picks harvested/harvest_partial that
    way — a Constant-only scan would miss the whole terminator)."""
    stages = _decision_stages()
    assert len(stages) > 10
    assert {"harvested", "harvest_partial"} <= stages
    assert "needs_resolution" in stages


def test_audit_fires_on_an_uncovered_boundary() -> None:
    """The guard can actually FIRE: a synthetic park with no target and no allowlist
    entry is reported, and adding the entry clears it. Without this, every assertion
    above would still pass if ``_uncovered`` were mutated to return nothing."""
    synthetic: dict[tuple[str, str], str | None] = {("made-up-block", "made_up_stage"): None}
    assert _uncovered(synthetic, ALLOWLIST) == [("made-up-block", "made_up_stage")]
    assert _uncovered(synthetic, {**ALLOWLIST, ("made-up-block", "made_up_stage"): "why"}) == []
    # A boundary WITH a target is never reported, allowlist or not.
    assert _uncovered({("made-up-block", "made_up_stage"): "somewhere"}, {}) == []


def test_allowlist_entries_are_live() -> None:
    """A stale allowlist entry is a hole: it pre-approves a boundary that no longer
    exists, ready to silently cover a future omission at the same key."""
    seen = _park_boundaries()
    stale = [key for key in ALLOWLIST if key not in seen]
    assert not stale, f"allowlist entries with no matching park boundary: {stale}"


def test_allowlist_entries_are_not_secretly_covered() -> None:
    """The inverse hole: an entry whose boundary DID gain a materialized default is
    now a lie in the ledger — the human can answer with one keystroke and the reason
    text says they cannot. Drop the entry when that happens."""
    seen = _park_boundaries()
    covered = [key for key in ALLOWLIST if seen.get(key)]
    assert not covered, (
        "allowlisted boundary/-ies now resolve a greenlight target — a bare `y` "
        f"works there, so the stated reason is stale: {covered}"
    )


def test_every_allowlist_reason_is_substantive() -> None:
    """A one-word reason is the same hole as no reason."""
    for key, reason in ALLOWLIST.items():
        assert len(reason.split()) >= 8, f"allowlist reason for {key} states no real reason"


def test_anomaly_terminators_disclose_their_override() -> None:
    """The two ANOMALY_TERMINATORS are the sharp case: ``block_chain`` declares that a
    bare ``y`` there "has no successor", yet the shared boundary rule DOES resolve a
    chain-forward override, so consumption advances. They therefore stay off the
    allowlist (leg (a) is factually satisfied) — and the menu must label the advance
    an OVERRIDE so the human is never one keystroke from a surprise."""
    boundaries = _park_boundaries()
    for verb, stage in sorted(block_chain.ANOMALY_TERMINATORS):
        assert (verb, stage) in boundaries, f"{(verb, stage)} is no longer a park boundary"
        target = boundaries[(verb, stage)]
        assert target, f"{(verb, stage)} lost its override target — allowlist it with a reason"
        menu = compose_answer_menu(
            brief={}, block=verb, run_id="r1", target=target, is_anomaly_terminator=True
        )
        assert menu is not None
        assert menu["options"][0]["override"] is True
        assert "OVERRIDE" in menu["options"][0]["means"]
