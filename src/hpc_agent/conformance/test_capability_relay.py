"""Conformance kit K5 — capability 2 (relay enforcement / the ACT half).

Asserts a harness's relay ENFORCEMENT POINT (``run_enforcement_point``) forces
exactly one continuation over a CONTRADICTING final message and passes a
FAITHFUL one — the ACT half of contract capability 2 (INSPECT, reading the
final text, is the adapter's business; the kit hands it the text and judges
only the ACT). The SAME ``(journal state, final message, expected verdict)``
triples (``fixtures/relay/triples.json``, loaded via
``conformance.relay_fixtures``) certify both conforming ACT shapes — Stop hooks
and a response gateway — because the seam is outcome-shaped
(``EnforcementOutcome``), never mechanism-shaped (D-K3 / the boundary-drift
"asserts outcomes, never mechanisms" flag).

Coverage (every blocking kind + passes + loop-safety + fail-open):

* CONTRADICTIONS block once, with a reason — ``number`` (a wrong count),
  ``state`` (``running`` over ``failed``), ``run_id`` (an unknown run/job id),
  and the NOTEBOOK forms (a wrong section status / module ``passed`` →
  ``state``; a mismatched sha-hex → ``number``);
* PASSES never block — a faithful relay, a truncation-prefix decimal, an
  ``unverifiable`` claim the seam drops, a message naming no run/audit;
* LOOP SAFETY — the same contradicting triple with ``previously_blocked=True``
  never blocks again (block at most once, never hard-block a session — the
  ``stop_hook_active`` re-entry convention);
* FAIL-OPEN — a torn (non-JSON) journal record degrades to not-blocked, and an
  unparseable render (a corrupt transcript) is a clean no-op.

Standalone seam (the K2 pattern): with no ``--harness-adapter`` the built-in
REFERENCE enforcement — Claude Code's Stop seam
(``relay_audit_stop.build_hook_output`` over a synthetic transcript) — is the
candidate, so this module is runnable on its own
(``pytest src/hpc_agent/conformance/test_capability_relay.py``). When an adapter
is supplied AND declares relay-enforcement, the adapter's
``run_enforcement_point`` is the candidate instead (a partial harness SKIPs,
carrying the contract-named degraded tier).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from hpc_agent.conformance.adapter import (
    CAP_RELAY_ENFORCEMENT,
    EnforcementOutcome,
    declared_capabilities,
)
from hpc_agent.conformance.relay_fixtures import RelayTriple, load_triples, seed_triple

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_TRIPLES = load_triples()
_BLOCKING = [t for t in _TRIPLES if t.blocks]


def _ids(triples: list[RelayTriple]) -> list[str]:
    return [t.name for t in triples]


# --- the enforcement candidate seam ------------------------------------------


@dataclass(frozen=True)
class EnforcementCandidate:
    """A relay ACT seam under test — the reference Stop hook or an adapter."""

    name: str
    run: Callable[..., EnforcementOutcome]


def _builtin_reference() -> EnforcementCandidate:
    """The Claude Code Stop seam driven in-process over a synthetic transcript.

    This is the reference ACT shape (hooks): write the final message as the
    trailing assistant entry of a JSONL transcript, build the Stop payload
    (``stop_hook_active`` models the re-entry), and map
    ``build_hook_output`` -> ``EnforcementOutcome`` (a returned block dict is a
    forced continuation; ``None`` is a pass).
    """
    from hpc_agent._kernel.hooks.relay_audit_stop import build_hook_output

    def run(
        experiment_dir: Path, final_message: str, *, previously_blocked: bool = False
    ) -> EnforcementOutcome:
        transcript = experiment_dir / "_kit_transcript.jsonl"
        content = [{"type": "text", "text": final_message}]
        line = json.dumps(
            {"type": "assistant", "message": {"role": "assistant", "content": content}}
        )
        transcript.write_text(line + "\n", encoding="utf-8")
        payload = {
            "cwd": str(experiment_dir),
            "transcript_path": str(transcript),
            "stop_hook_active": previously_blocked,
        }
        out = build_hook_output(payload)
        if out is None:
            return EnforcementOutcome(blocked=False, reason=None)
        return EnforcementOutcome(blocked=True, reason=out.get("reason"))

    return EnforcementCandidate(name="hpc-agent (Stop hook)", run=run)


@pytest.fixture
def enforcement_candidate(request: pytest.FixtureRequest) -> EnforcementCandidate:
    """The ACT seam to certify — the adapter's when supplied, else the reference.

    With ``--harness-adapter`` the adapter's ``run_enforcement_point`` is the
    candidate (SKIP with the degraded tier when the adapter does not declare
    relay-enforcement). Standalone, the built-in reference Stop seam runs so the
    module certifies the vectors against our own implementation.
    """
    spec = request.config.getoption("--harness-adapter", default=None)
    if spec:
        adapter = request.getfixturevalue("harness_adapter")
        if CAP_RELAY_ENFORCEMENT not in declared_capabilities(adapter):
            request.getfixturevalue("require_relay_enforcement")  # SKIP with the tier
        return EnforcementCandidate(
            name=getattr(adapter, "name", "<adapter>"), run=adapter.run_enforcement_point
        )
    return _builtin_reference()


# --- assertions --------------------------------------------------------------


@pytest.mark.parametrize("triple", _TRIPLES, ids=_ids(_TRIPLES))
def test_enforcement_matches_expected(
    triple: RelayTriple, enforcement_candidate: EnforcementCandidate, fixture_repo: Path
) -> None:
    """A contradicting relay blocks (with a reason); a faithful one does not.

    The outcome is judged against the triple's DERIVED ``blocks`` — derived in
    turn from the hook's own ``_CONTRADICTION_KINDS`` — so a blocking-set change
    can never silently desync this assertion from the code.
    """
    seed_triple(fixture_repo, triple)
    outcome = enforcement_candidate.run(fixture_repo, triple.final_message)

    assert outcome.blocked is triple.blocks, (
        f"[{enforcement_candidate.name}] {triple.name}: expected "
        f"blocked={triple.blocks} (kinds {triple.contradiction_kinds}), got "
        f"{outcome.blocked} — {triple.doc}"
    )
    if triple.blocks:
        assert outcome.reason, f"{triple.name}: a block must carry an itemized reason"
    else:
        assert not outcome.reason, f"{triple.name}: a pass must carry no block reason"


@pytest.mark.parametrize("triple", _BLOCKING, ids=_ids(_BLOCKING))
def test_loop_safety_never_blocks_twice(
    triple: RelayTriple, enforcement_candidate: EnforcementCandidate, fixture_repo: Path
) -> None:
    """``previously_blocked=True`` never blocks again — block at most once.

    The ``stop_hook_active`` re-entry convention: a conforming seam forces the
    FIRST continuation and then lets the (corrected or even uncorrected) relay
    through, so it can never hard-block a session. Every contradicting triple —
    which blocks on the first pass (asserted above) — must pass on re-entry.
    """
    seed_triple(fixture_repo, triple)
    outcome = enforcement_candidate.run(fixture_repo, triple.final_message, previously_blocked=True)
    assert outcome.blocked is False, (
        f"[{enforcement_candidate.name}] {triple.name}: blocked twice — a seam "
        "must never block a stop that is already a hook-forced continuation"
    )


def _triple(name: str) -> RelayTriple:
    return next(t for t in _TRIPLES if t.name == name)


def test_failopen_torn_journal_does_not_block(
    enforcement_candidate: EnforcementCandidate, fixture_repo: Path
) -> None:
    """A torn (non-JSON) run record degrades to not-blocked — AND the guard can
    fire: the same 'running' claim blocks when the record reads intact."""
    torn = _triple("run_torn_record_failopen")
    seed_triple(fixture_repo, torn)
    assert enforcement_candidate.run(fixture_repo, torn.final_message).blocked is False

    # Guard-can-fire: the intact-record counterpart with the SAME kind of state
    # claim DOES block, so the fail-open above is graceful degradation, not a
    # dead assertion.
    intact = _triple("run_state_contradiction")
    assert intact.blocks is True


def test_failopen_unparseable_render_is_noop(fixture_repo: Path) -> None:
    """An unparseable render (a corrupt transcript) is a clean no-op.

    Seam-level (reference only): a torn transcript yields no final text, so the
    Stop seam passes rather than raising into the harness. A blocking triple is
    seeded first, proving it is the TORN RENDER — not an empty journal — that
    suppresses the block.
    """
    from hpc_agent._kernel.hooks.relay_audit_stop import build_hook_output

    seed_triple(fixture_repo, _triple("run_state_contradiction"))
    transcript = fixture_repo / "_torn_transcript.jsonl"
    transcript.write_text("{ not json at all\n\x00\x01", encoding="utf-8")
    payload = {"cwd": str(fixture_repo), "transcript_path": str(transcript)}
    assert build_hook_output(payload) is None
