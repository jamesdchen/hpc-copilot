"""Per-capability declaration + skip reasons (K1 machinery unit test).

Declaration is by IMPLEMENTATION (no manifest); a skip reason for an undeclared
capability must carry the CONTRACT-NAMED degraded tier verbatim — the honest-
skip posture the report depends on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hpc_agent.conformance.adapter import (
    CAP_BACKGROUNDING,
    CAP_RELAY_ENFORCEMENT,
    CAP_UTTERANCE_LOG,
    DEGRADED_TIERS,
    EnforcementOutcome,
    WakeEvent,
    declared_capabilities,
    skip_reason_for,
)


class _FullAdapter:
    name = "full"

    def write_utterance(self, experiment_dir: Path, text: str) -> None: ...

    def run_enforcement_point(
        self, experiment_dir: Path, final_message: str, *, previously_blocked: bool = False
    ) -> EnforcementOutcome:
        return EnforcementOutcome(False, None)

    def start_background(self, experiment_dir: Path, argv: list[str]) -> Any:
        return object()

    def await_wake(self, handle: Any, timeout_s: float) -> WakeEvent:
        return WakeEvent(True, True)


class _UtteranceOnlyAdapter:
    name = "partial"

    def write_utterance(self, experiment_dir: Path, text: str) -> None: ...


class _NoBackgroundAwaitAdapter:
    """start_background present but await_wake missing → backgrounding UNdeclared
    (a capability needs ALL its methods)."""

    name = "half-bg"

    def write_utterance(self, experiment_dir: Path, text: str) -> None: ...

    def run_enforcement_point(
        self, experiment_dir: Path, final_message: str, *, previously_blocked: bool = False
    ) -> EnforcementOutcome:
        return EnforcementOutcome(False, None)

    def start_background(self, experiment_dir: Path, argv: list[str]) -> Any:
        return object()


def test_full_adapter_declares_all_three() -> None:
    assert declared_capabilities(_FullAdapter()) == frozenset(
        {CAP_UTTERANCE_LOG, CAP_RELAY_ENFORCEMENT, CAP_BACKGROUNDING}
    )


def test_partial_adapter_declares_only_utterance() -> None:
    assert declared_capabilities(_UtteranceOnlyAdapter()) == frozenset({CAP_UTTERANCE_LOG})


def test_backgrounding_needs_both_methods() -> None:
    declared = declared_capabilities(_NoBackgroundAwaitAdapter())
    assert CAP_BACKGROUNDING not in declared
    assert declared == frozenset({CAP_UTTERANCE_LOG, CAP_RELAY_ENFORCEMENT})


@pytest.mark.parametrize(
    "capability",
    [CAP_UTTERANCE_LOG, CAP_RELAY_ENFORCEMENT, CAP_BACKGROUNDING],
)
def test_skip_reason_carries_contract_tier(capability: str) -> None:
    reason = skip_reason_for(capability)
    assert capability in reason
    assert DEGRADED_TIERS[capability] in reason


def test_degraded_tiers_are_the_three_contract_tiers() -> None:
    # exact wording from harness-contract.md's "Degrades when absent" clauses
    assert DEGRADED_TIERS[CAP_UTTERANCE_LOG] == "journal-response friction tier"
    assert DEGRADED_TIERS[CAP_RELAY_ENFORCEMENT] == "verb-only relay-audit posture"
    assert (
        DEGRADED_TIERS[CAP_BACKGROUNDING] == "synchronous in-turn execution; correctness unaffected"
    )
