"""K6 negotiation — mirror unit test (green against a reference adapter).

Two halves, mirroring the shipped ``test_negotiation.py``:

* the ADAPTER legs (``declared == detected == behaved`` per seam) driven against
  a reference adapter that DECLARES all three capabilities and DETECTS them by
  behavior — proving the shipped negotiation assertions pass for a conforming
  harness, plus a mismatched adapter (detects a capability it does not declare)
  that the kit correctly FAILS (guard-can-fire);
* the ELICITATION legs (E7) re-run for real against the in-repo duplex rig
  (``tests/_mcp_harness.py``), which is importable here (unlike from the wheel),
  so the client-declared / server-detected / fires-when-true chain is exercised
  end-to-end in core CI.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import NamedTuple

import pytest

from hpc_agent.conformance import test_capability_backgrounding as bg_kit
from hpc_agent.conformance import test_negotiation as kit
from hpc_agent.conformance.adapter import (
    CAP_BACKGROUNDING,
    CAP_RELAY_ENFORCEMENT,
    CAP_UTTERANCE_LOG,
    EnforcementOutcome,
    WakeEvent,
)
from hpc_agent.conformance.fixture_repo import claim_fixture_repo
from hpc_agent.state.utterances import append_utterance, read_utterances, utterances_path


class _BgHandle(NamedTuple):
    proc: subprocess.Popen[bytes]
    experiment_dir: Path


class _ReferenceAdapter:
    """A conforming reference harness: declares all three capabilities and
    detects each seam BY BEHAVING it (the honest-detection posture for a non-
    Claude-Code harness — the write path proves the reader accepts what it wrote,
    the relay seam proves its loop-safety invariant)."""

    name = "reference"

    def write_utterance(self, experiment_dir: Path, text: str) -> None:
        append_utterance(experiment_dir, text)

    def run_enforcement_point(
        self, experiment_dir: Path, final_message: str, *, previously_blocked: bool = False
    ) -> EnforcementOutcome:
        if previously_blocked:
            return EnforcementOutcome(False, None)  # never block twice
        if "CONTRADICTION" in final_message:
            return EnforcementOutcome(True, "reference: contradiction detected")
        return EnforcementOutcome(False, None)

    def start_background(self, experiment_dir: Path, argv: list[str]) -> _BgHandle:
        proc = subprocess.Popen(argv, cwd=str(experiment_dir), env=os.environ.copy())
        return _BgHandle(proc, Path(experiment_dir))

    def await_wake(self, handle: _BgHandle, timeout_s: float) -> WakeEvent:
        try:
            handle.proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            handle.proc.kill()
            return WakeEvent(False, False)
        terminal = utterances_path(handle.experiment_dir).parent / bg_kit.STUB_TERMINAL_NAME
        return WakeEvent(True, terminal.exists())

    def detect_capabilities(self, experiment_dir: Path) -> frozenset[str]:
        detected = {CAP_BACKGROUNDING}  # core-side constant
        probe = "reference detect-by-behavior probe"
        self.write_utterance(experiment_dir, probe)
        if any(r["text"] == probe for r in read_utterances(experiment_dir)):
            detected.add(CAP_UTTERANCE_LOG)
        if self.run_enforcement_point(experiment_dir, "x CONTRADICTION y").blocked:
            detected.add(CAP_RELAY_ENFORCEMENT)
        return frozenset(detected)


class _OverclaimingAdapter(_ReferenceAdapter):
    """DETECTS relay-enforcement it does NOT implement — the detected-but-not-
    declared mismatch the kit exists to catch."""

    name = "overclaiming"
    run_enforcement_point = None  # type: ignore[assignment]  # not implemented → undeclared

    def detect_capabilities(self, experiment_dir: Path) -> frozenset[str]:
        return frozenset({CAP_UTTERANCE_LOG, CAP_RELAY_ENFORCEMENT, CAP_BACKGROUNDING})


def _claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    repo: Path = claim_fixture_repo(tmp_path / "experiment")
    return repo


# ─── the adapter legs against the reference (all green) ──────────────────────


def test_reference_passes_projection_leg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _claim(tmp_path, monkeypatch)
    kit.test_detected_projects_onto_contract_nouns(_ReferenceAdapter(), repo)


def test_reference_passes_declared_equals_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _claim(tmp_path, monkeypatch)
    adapter = _ReferenceAdapter()
    kit.test_declared_seam_caps_are_detected(adapter, repo)
    kit.test_detected_seam_caps_are_declared(adapter, repo)


def test_reference_passes_behaved_legs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _claim(tmp_path, monkeypatch)
    adapter = _ReferenceAdapter()
    kit.test_declared_utterance_log_behaves(adapter, repo, None)
    kit.test_declared_relay_never_blocks_twice(adapter, repo, None)
    kit.test_declared_backgrounding_behaves(adapter, repo, None)


# ─── guard-can-fire: an overclaiming adapter is FAILED by the kit ────────────


def test_kit_fails_detected_but_not_declared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An adapter that DETECTS relay it never implemented trips the detected-but-
    not-declared assertion — the negotiation guard actually fires."""
    repo = _claim(tmp_path, monkeypatch)
    with pytest.raises(AssertionError, match="detected but undeclared"):
        kit.test_detected_seam_caps_are_declared(_OverclaimingAdapter(), repo)


# ─── the elicitation legs (E7), re-run for real in-repo ──────────────────────


def test_elicitation_fires_when_client_supports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kit.test_elicitation_declared_detected_behaved_when_client_supports(tmp_path, monkeypatch)


def test_elicitation_absent_when_client_silent() -> None:
    kit.test_elicitation_absent_when_client_silent()


def test_harness_capabilities_report_is_honest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kit.test_harness_capabilities_reports_elicitation_honestly(tmp_path, monkeypatch)
