"""Conformance kit K6 — negotiation: ``declared == detected == behaved``.

The kit's closing assertion (``docs/design/conformance-kit.md``, "Negotiation" +
``docs/internals/harness-contract.md``, "Capability negotiation"): a harness's
capability set is DETECTED, never a self-asserted manifest, and the three legs
must be ONE set —

* **declared** — the adapter's implemented-method set
  (:func:`~hpc_agent.conformance.adapter.declared_capabilities`);
* **detected** — what the seams observe
  (``adapter.detect_capabilities`` → the ``harness-capabilities`` verb projection
  onto the three contract nouns; ``trusted_display`` is excluded — the projection
  rule);
* **behaved** — the capabilities whose behavior the kit actually exercises.

A drift between any two is the bug the kit exists to catch: a
detected-but-not-behaved capability, or a behaved-but-not-detected one. Honest
partials are NOT failures — an undeclared capability is skipped (its module
degraded-tier), and negotiation fails only on three-way DISAGREEMENT for a
capability the harness DOES claim.

**Which detection leg is a per-harness SEAM vs a core-side CONSTANT** (the
honest-detection rule): ``backgrounding`` detection is a core-side constant
(always true — the detached-worker path is core), so the kit asserts only its
BEHAVED leg, never a per-harness detection; ``utterance-log`` and
``relay-enforcement`` are per-harness SEAMS whose ``declared`` and ``detected``
sets must AGREE.

(The former E7 elicitation leg — the MCP-elicitation second capability-1
channel's per-session negotiation — was retired with that channel on
2026-07-27: the hook/chat path is THE capability-1 channel, so there is no
per-session negotiation left to assert.)
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from hpc_agent.conformance.adapter import (
    CAP_RELAY_ENFORCEMENT,
    CAP_UTTERANCE_LOG,
    CAPABILITIES,
    declared_capabilities,
)
from hpc_agent.conformance.test_capability_backgrounding import (
    STUB_TERMINAL_NAME,
    stub_worker_argv,
)
from hpc_agent.state.utterances import read_utterances

if TYPE_CHECKING:
    from hpc_agent.conformance.adapter import HarnessAdapter

# The per-harness SEAM capabilities whose declared and detected sets must agree.
# backgrounding is EXCLUDED: its detection is a core-side constant (always true),
# so only its behaved leg is asserted (the honest-detection rule).
SEAMS: frozenset[str] = frozenset({CAP_UTTERANCE_LOG, CAP_RELAY_ENFORCEMENT})

_BEHAVED_UTTERANCE_PROBE = "conformance negotiation behaved-leg probe"


# ─── the adapter negotiation legs (declared == detected, per seam) ───────────


def test_detected_projects_onto_contract_nouns(
    harness_adapter: HarnessAdapter, fixture_repo: Path
) -> None:
    """Detection reports ONLY the three contract nouns — never the raw four.

    The projection rule: ``harness-capabilities`` reports four capabilities, one
    of which (``trusted_display``) is always ``"unknown"`` and has no kit noun;
    the negotiation set is the projection onto ``{utterance-log,
    relay-enforcement, backgrounding}``.
    """
    detected = harness_adapter.detect_capabilities(fixture_repo)
    assert detected <= CAPABILITIES, (
        f"detect_capabilities leaked a non-contract noun: {detected - CAPABILITIES}"
    )


def test_declared_seam_caps_are_detected(
    harness_adapter: HarnessAdapter, fixture_repo: Path
) -> None:
    """A declared SEAM capability the detection MISSES is behaved-but-not-detected."""
    declared = declared_capabilities(harness_adapter) & SEAMS
    detected = harness_adapter.detect_capabilities(fixture_repo) & SEAMS
    missing = declared - detected
    assert not missing, f"declared but undetected seam capabilities: {sorted(missing)}"


def test_detected_seam_caps_are_declared(
    harness_adapter: HarnessAdapter, fixture_repo: Path
) -> None:
    """A detected SEAM capability the adapter does NOT implement is detected-but-
    not-declared — detection claiming a capability the harness cannot behave."""
    declared = declared_capabilities(harness_adapter) & SEAMS
    detected = harness_adapter.detect_capabilities(fixture_repo) & SEAMS
    extra = detected - declared
    assert not extra, f"detected but undeclared seam capabilities: {sorted(extra)}"


def test_declared_utterance_log_behaves(
    harness_adapter: HarnessAdapter,
    fixture_repo: Path,
    require_utterance_log: None,  # noqa: ARG001 — skip-with-tier gate
) -> None:
    """behaved leg for capability 1: a written utterance round-trips through the
    reader — the write channel proves the reader accepts what it wrote."""
    harness_adapter.write_utterance(fixture_repo, _BEHAVED_UTTERANCE_PROBE)
    texts = [record["text"] for record in read_utterances(fixture_repo)]
    assert _BEHAVED_UTTERANCE_PROBE in texts, "write_utterance did not land in the reader's log"


def test_declared_relay_never_blocks_twice(
    harness_adapter: HarnessAdapter,
    fixture_repo: Path,
    require_relay_enforcement: None,  # noqa: ARG001 — skip-with-tier gate
) -> None:
    """behaved leg for capability 2: the universal loop-safety invariant a
    conforming ACT seam MUST satisfy regardless of message — a re-entry
    (``previously_blocked=True``) never blocks again (block at most once)."""
    outcome = harness_adapter.run_enforcement_point(
        fixture_repo, "any final agent-visible message", previously_blocked=True
    )
    assert outcome.blocked is False, "a conforming relay seam must never block twice"


def test_declared_backgrounding_behaves(
    harness_adapter: HarnessAdapter,
    fixture_repo: Path,
    require_backgrounding: None,  # noqa: ARG001 — skip-with-tier gate
) -> None:
    """behaved leg for capability 3 (the core-side constant): the stub worker
    wakes the driver and the wake sees the terminal — asserted BEHAVED-only, the
    negotiation set never carries a per-harness backgrounding DETECTION."""
    handle = harness_adapter.start_background(fixture_repo, stub_worker_argv(fixture_repo))
    wake = harness_adapter.await_wake(handle, 30.0)
    assert wake.woke and wake.terminal_seen, "backgrounding declared but did not behave"


# Referenced so a stale rename of the rendezvous constant fails loudly here too
# (the mirror and the reference adapter both key off this name).
assert STUB_TERMINAL_NAME == "stub_worker.terminal.json"
