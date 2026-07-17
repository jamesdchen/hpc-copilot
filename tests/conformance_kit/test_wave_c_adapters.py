"""Wave-C second-harness proofs — mirror unit tests (green in core CI).

Drives the SHIPPED kit assertion functions against the three NON-Claude reference
adapters (``docs/plans/anti-vendor-lockout-2026-07-17.md`` §2/Wave C), proving
capabilities 2 & 3 have a conforming exercise outside Claude Code's hook model,
plus the guard-can-fire direction (a planted non-conforming gateway that delivers
BEFORE the verdict FAILS the ACT bar). These run in the core ``test`` job (fast,
in-process); the subprocess-level verdict pin is the ``conformance:`` CI matrix.

* **response-gateway (capability 2 / ACT)** — its ``run_enforcement_point`` holds
  a contradicting relay back via ``verify_relay`` pre-delivery, no Stop hook; it
  passes every ACT triple and the loop-safety invariant.
* **otel-genai (capability 2 / INSPECT)** — its ``inspect_relay`` DETECTS every
  contradicting triple and reports it via a GenAI span, never blocking — the
  honest weaker tier.
* **foreign-backgrounding (capability 3)** — a plain-subprocess detach/wake passes
  the detached-lifecycle assertion.

The env-var note: the reference Stop hook's completer path keys off
``HPC_STOP_HOOK_APPEND`` — set in some dev shells — which reshapes ``claude_code``'s
output. These three adapters are IMMUNE (they call ``verify_relay`` /
``subprocess`` directly, never ``build_hook_output``), so this file needs no env
scrubbing; the ``claude_code`` self-run pins live in ``test_self_run_adapters.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_agent.conformance import test_capability_backgrounding as bg_kit
from hpc_agent.conformance import test_capability_relay as relay_kit
from hpc_agent.conformance import test_capability_relay_inspect as inspect_kit
from hpc_agent.conformance.adapter import (
    CAP_BACKGROUNDING,
    CAP_RELAY_ENFORCEMENT,
    CAP_RELAY_INSPECT,
    EnforcementOutcome,
    declared_capabilities,
)
from hpc_agent.conformance.adapters.foreign_backgrounding import ForeignBackgroundingAdapter
from hpc_agent.conformance.adapters.otel_genai import OtelGenAiAdapter
from hpc_agent.conformance.adapters.response_gateway import ResponseGatewayAdapter
from hpc_agent.conformance.fixture_repo import claim_fixture_repo
from hpc_agent.conformance.relay_fixtures import RelayTriple, load_triples

_TRIPLES = load_triples()
_BLOCKING = [t for t in _TRIPLES if t.blocks]


def _claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    return claim_fixture_repo(tmp_path / "experiment")


# ─── response-gateway: capability 2, the ACT half, no hooks ──────────────────


def test_gateway_declares_only_relay_enforcement() -> None:
    """The gateway declares the ACT bar and nothing else — honest partial."""
    assert declared_capabilities(ResponseGatewayAdapter()) == frozenset({CAP_RELAY_ENFORCEMENT})


@pytest.mark.parametrize("triple", _TRIPLES, ids=[t.name for t in _TRIPLES])
def test_gateway_passes_shipped_act_assertion(
    triple: RelayTriple, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SHIPPED ACT assertion passes driven by the gateway: a contradicting
    relay blocks (with a reason), a faithful one does not — the verify_relay
    pre-delivery pass matches the reference Stop seam outcome-for-outcome."""
    repo = _claim(tmp_path, monkeypatch)
    adapter = ResponseGatewayAdapter()
    candidate = relay_kit.EnforcementCandidate(name=adapter.name, run=adapter.run_enforcement_point)
    relay_kit.test_enforcement_matches_expected(triple, candidate, repo)


@pytest.mark.parametrize("triple", _BLOCKING, ids=[t.name for t in _BLOCKING])
def test_gateway_never_blocks_twice(
    triple: RelayTriple, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loop-safety: a message the gateway already held once is delivered (block at
    most once) — the ``stop_hook_active`` analogue for a pre-delivery gateway."""
    repo = _claim(tmp_path, monkeypatch)
    adapter = ResponseGatewayAdapter()
    candidate = relay_kit.EnforcementCandidate(name=adapter.name, run=adapter.run_enforcement_point)
    relay_kit.test_loop_safety_never_blocks_twice(triple, candidate, repo)


def test_gateway_detects_relay_enforcement_by_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Detection by BEHAVING (D-K3): the gateway seeds a known contradiction and
    confirms its own gate holds it back — no hook needle."""
    repo = _claim(tmp_path, monkeypatch)
    assert ResponseGatewayAdapter().detect_capabilities(repo) == frozenset({CAP_RELAY_ENFORCEMENT})


# ─── the planted NON-conforming gateway: FAILS the ACT bar (guard-can-fire) ───


class _LeakyGateway:
    """A BROKEN gateway that delivers the response BEFORE the verify_relay verdict —
    it never holds anything back. The ACT bar exists to catch exactly this."""

    name = "leaky-gateway (planted non-conforming)"

    def run_enforcement_point(
        self, experiment_dir: Path, final_message: str, *, previously_blocked: bool = False
    ) -> EnforcementOutcome:
        return EnforcementOutcome(blocked=False, reason=None)  # delivered pre-verdict


def test_planted_leaky_gateway_fails_the_act_bar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gateway that delivers before verifying FAILS ``test_enforcement_matches_expected``
    on a contradicting triple — the ACT assertion actually fires against a fake."""
    repo = _claim(tmp_path, monkeypatch)
    leaky = _LeakyGateway()
    candidate = relay_kit.EnforcementCandidate(name=leaky.name, run=leaky.run_enforcement_point)
    contradicting = next(t for t in _BLOCKING if t.scope == "run")
    with pytest.raises(AssertionError, match="expected blocked=True"):
        relay_kit.test_enforcement_matches_expected(contradicting, candidate, repo)


# ─── otel-genai: capability 2, the INSPECT half (the disclosed weaker tier) ───


def test_otel_declares_only_relay_inspection() -> None:
    """The OTel harness declares the WEAKER INSPECT tier and NOT the ACT bar — so
    the kit records it honestly, never rounding INSPECT up to a false ACT pass."""
    declared = declared_capabilities(OtelGenAiAdapter())
    assert declared == frozenset({CAP_RELAY_INSPECT})
    assert CAP_RELAY_ENFORCEMENT not in declared


@pytest.mark.parametrize("triple", _TRIPLES, ids=[t.name for t in _TRIPLES])
def test_otel_passes_shipped_inspect_assertion(
    triple: RelayTriple, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SHIPPED INSPECT assertion passes driven by the OTel adapter: it DETECTS
    every contradicting triple and nothing else, never blocking."""
    repo = _claim(tmp_path, monkeypatch)
    inspect_kit.test_inspect_detects_contradictions_never_passes(
        triple, OtelGenAiAdapter(), repo, None
    )


def test_otel_emits_a_genai_span_on_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """INSPECT DISCLOSES via telemetry: a detected contradiction lands a GenAI span
    carrying the observable final message + the contradiction verdict."""
    repo = _claim(tmp_path, monkeypatch)
    adapter = OtelGenAiAdapter()
    triple = next(t for t in _BLOCKING if t.scope == "run")
    from hpc_agent.conformance.relay_fixtures import seed_triple

    seed_triple(repo, triple)
    outcome = adapter.inspect_relay(repo, triple.final_message)
    assert outcome.detected is True
    assert adapter.spans, "an INSPECT harness must emit an observability span"
    span = adapter.spans[-1]
    assert span["attributes"]["gen_ai.evaluation.relay.contradicted"] is True
    assert span["attributes"]["gen_ai.response.final_message"] == triple.final_message


# ─── foreign-backgrounding: capability 3, no Claude machinery ─────────────────


def test_foreign_bg_declares_only_backgrounding() -> None:
    assert declared_capabilities(ForeignBackgroundingAdapter()) == frozenset({CAP_BACKGROUNDING})


def test_foreign_bg_passes_shipped_backgrounding_assertion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SHIPPED capability-3 assertion passes driven by a plain-subprocess
    detach/wake: started work wakes the driver and the wake sees the terminal."""
    repo = _claim(tmp_path, monkeypatch)
    bg_kit.test_started_work_wakes_and_sees_terminal(ForeignBackgroundingAdapter(), repo, None)


def test_foreign_bg_detects_backgrounding() -> None:
    assert ForeignBackgroundingAdapter().detect_capabilities(Path(".")) == frozenset(
        {CAP_BACKGROUNDING}
    )
