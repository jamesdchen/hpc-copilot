"""Conformance kit — capability 2, the INSPECT half (the weaker, disclosed tier).

Asserts a harness's relay INSPECT seam (``inspect_relay``) OBSERVES the final
agent-visible message and REPORTS a contradiction WITHOUT enforcing — the INSPECT
half of contract capability 2 (``docs/internals/harness-contract.md``, "Capability
2, split: INSPECT vs ACT"). This is the honest weaker tier: an INSPECT harness
DETECTS a contradiction but never forces a continuation, so the ENFORCEMENT
guarantee stays verb-only. The kit records that faithfully — a harness declaring
``inspect_relay`` but NOT ``run_enforcement_point`` earns the disclosed
:data:`~hpc_agent.conformance.adapter.CAP_RELAY_INSPECT` tier here while the ACT
bar (``relay-enforcement``) is SKIPPED at its own degraded tier by the standard
``require_relay_enforcement`` gate — INSPECT is never rounded up to a false ACT
pass.

It runs against the SAME ``(journal state, final message, expected verdict)``
triples the ACT module uses (``fixtures/relay/triples.json``): a conforming
INSPECT seam DETECTS exactly the CONTRADICTING triples (``triple.blocks``, derived
from the hook's own ``_CONTRADICTION_KINDS``) and detects NOTHING on the faithful
/ ``unverifiable`` / names-nothing passes — the same discrimination the ACT seam
blocks on, minus the block.

Adapter-driven only (the K6 backgrounding pattern): there is no built-in INSPECT
reference — INSPECT is a foreign-harness shape (OTel-GenAI telemetry), so
``require_relay_inspect`` SKIPS the module WITH the contract-named degraded tier
when the adapter does not declare it. Because relay-inspection is an OPTIONAL
weaker tier (NOT one of the three core contract capabilities), that skip is
DELIBERATELY not tallied into the core conformance verdict (see
``conftest.require_relay_inspect``): a harness that provides the STRONGER ACT bar,
or a different capability entirely, is not marked down for lacking this add-on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hpc_agent.conformance.relay_fixtures import RelayTriple, load_triples, seed_triple

if TYPE_CHECKING:
    from pathlib import Path

    from hpc_agent.conformance.adapter import HarnessAdapter

_TRIPLES = load_triples()
_BLOCKING = [t for t in _TRIPLES if t.blocks]


def _ids(triples: list[RelayTriple]) -> list[str]:
    return [t.name for t in triples]


@pytest.mark.parametrize("triple", _TRIPLES, ids=_ids(_TRIPLES))
def test_inspect_detects_contradictions_never_passes(
    triple: RelayTriple,
    harness_adapter: HarnessAdapter,
    fixture_repo: Path,
    require_relay_inspect: None,  # noqa: ARG001 — skip-with-tier gate (conftest)
) -> None:
    """INSPECT reports a contradiction on every contradicting triple, nothing else.

    ``detected`` is judged against the triple's DERIVED ``blocks`` — the SAME
    contradiction set the ACT seam blocks on — so an INSPECT harness that saw a
    contradiction the ACT seam would block, or flagged a faithful relay, is caught.
    The seam NEVER blocks: the outcome carries no enforcement, only the observe-
    and-report verdict (the disclosed weaker tier).
    """
    seed_triple(fixture_repo, triple)
    outcome = harness_adapter.inspect_relay(fixture_repo, triple.final_message)

    assert outcome.detected is triple.blocks, (
        f"[{harness_adapter.name}] {triple.name}: expected detected={triple.blocks} "
        f"(kinds {triple.contradiction_kinds}), got {outcome.detected} — {triple.doc}"
    )
    if triple.blocks:
        assert outcome.report, f"{triple.name}: a detection must carry a disclosed report"
    else:
        assert not outcome.report, f"{triple.name}: a pass must carry no report"


def _triple(name: str) -> RelayTriple:
    return next(t for t in _TRIPLES if t.name == name)


def test_inspect_failopen_torn_journal_does_not_detect(
    harness_adapter: HarnessAdapter,
    fixture_repo: Path,
    require_relay_inspect: None,  # noqa: ARG001 — skip-with-tier gate (conftest)
) -> None:
    """A torn (non-JSON) run record degrades to not-detected — AND the seam can fire:
    the same 'running' claim IS detected when the record reads intact."""
    torn = _triple("run_torn_record_failopen")
    seed_triple(fixture_repo, torn)
    assert harness_adapter.inspect_relay(fixture_repo, torn.final_message).detected is False

    # Guard-can-fire: the intact-record counterpart with the SAME kind of state
    # claim IS detected, so the fail-open above is graceful degradation, not a
    # dead assertion.
    intact = _triple("run_state_contradiction")
    seed_triple(fixture_repo, intact)
    assert harness_adapter.inspect_relay(fixture_repo, intact.final_message).detected is True
