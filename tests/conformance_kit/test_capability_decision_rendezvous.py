"""Capability 7 (decision-rendezvous) — mirror unit test (core CI).

Drives the SHIPPED kit assertions
(``hpc_agent.conformance.test_capability_decision_rendezvous``) against the
REFERENCE rendezvous core (green — the behaved-for-the-reference-adapter leg) over
a seeded journal, AND against planted NON-conforming fakes the kit correctly FAILS
(guard-can-fire):

* a rendezvous that FIRES while merely awaiting the human trips the silence battery;
* a rendezvous that ignores ``previously_blocked`` (blocks twice) trips loop-safety;
* a rendezvous that NEVER fires trips the committed-unadvanced battery.

Plus: an adapter that IMPLEMENTS ``run_decision_rendezvous`` DECLARES capability 7
(the adapter seam a foreign provider uses in Wave C).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_agent.conformance import test_capability_decision_rendezvous as kit
from hpc_agent.conformance.adapter import (
    CAP_DECISION_RENDEZVOUS,
    EnforcementOutcome,
    declared_capabilities,
)
from hpc_agent.conformance.fixture_repo import claim_fixture_repo


def _fresh_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> Path:
    """A distinct claimed repo (isolated journal namespace) per check call.

    Pins the journal home once and clears the completer env markers so the
    reference guard takes the deterministic REJECTOR (force-continuation) path.
    """
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.delenv("HPC_STOP_HOOK_APPEND", raising=False)
    monkeypatch.delenv("HPC_STOP_HOOK_APPEND_ON_BLOCK", raising=False)
    repo: Path = claim_fixture_repo(tmp_path / name)
    return repo


# ─── the reference core passes all three shipped batteries ───────────────────


def test_reference_core_forces_on_committed_unadvanced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = kit._builtin_reference()
    kit.check_forces_on_committed_unadvanced(candidate, _fresh_repo(tmp_path, monkeypatch, "a"))


def test_reference_core_silent_while_merely_awaiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = kit._builtin_reference()
    kit.check_silent_while_merely_awaiting(candidate, _fresh_repo(tmp_path, monkeypatch, "b"))


def test_reference_core_loop_safe_reentry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    candidate = kit._builtin_reference()
    kit.check_loop_safe_reentry(candidate, _fresh_repo(tmp_path, monkeypatch, "c"))


# ─── guard-can-fire: non-conforming fakes are FAILED by the kit ──────────────


def _always_fires() -> kit.RendezvousCandidate:
    return kit.RendezvousCandidate(
        name="fake-always-fires",
        run=lambda _repo, *, previously_blocked=False: EnforcementOutcome(True, "always fires"),
    )


def _never_fires() -> kit.RendezvousCandidate:
    return kit.RendezvousCandidate(
        name="fake-never-fires",
        run=lambda _repo, *, previously_blocked=False: EnforcementOutcome(False, None),
    )


def test_fake_that_fires_while_awaiting_is_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rendezvous that fires while merely awaiting FAILS the silence battery."""
    repo = _fresh_repo(tmp_path, monkeypatch, "d")
    with pytest.raises(AssertionError, match="stay silent"):
        kit.check_silent_while_merely_awaiting(_always_fires(), repo)


def test_fake_that_blocks_twice_is_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A rendezvous that ignores ``previously_blocked`` FAILS loop-safety."""
    with pytest.raises(AssertionError, match="loop-safe re-entry"):
        kit.check_loop_safe_reentry(_always_fires(), _fresh_repo(tmp_path, monkeypatch, "e"))


def test_fake_that_never_fires_is_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A rendezvous that never fires FAILS the committed-unadvanced battery."""
    repo = _fresh_repo(tmp_path, monkeypatch, "f")
    with pytest.raises(AssertionError, match="force a continuation"):
        kit.check_forces_on_committed_unadvanced(_never_fires(), repo)


# ─── the adapter seam: implementing the method DECLARES capability 7 ─────────


class _RendezvousAdapter:
    """A minimal harness declaring ONLY capability 7 (the Wave-C adapter shape)."""

    name = "rendezvous-only"

    def run_decision_rendezvous(
        self, experiment_dir: Path, *, previously_blocked: bool = False
    ) -> EnforcementOutcome:
        return kit._builtin_reference().run(experiment_dir, previously_blocked=previously_blocked)


def test_adapter_implementing_run_decision_rendezvous_declares_capability_7() -> None:
    assert CAP_DECISION_RENDEZVOUS in declared_capabilities(_RendezvousAdapter())


def test_adapter_declaring_capability_7_passes_the_battery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A declared capability-7 adapter certifies through the SAME shipped batteries."""
    adapter = _RendezvousAdapter()
    candidate = kit.RendezvousCandidate(name=adapter.name, run=adapter.run_decision_rendezvous)
    kit.check_forces_on_committed_unadvanced(candidate, _fresh_repo(tmp_path, monkeypatch, "g"))
    kit.check_silent_while_merely_awaiting(candidate, _fresh_repo(tmp_path, monkeypatch, "h"))
    kit.check_loop_safe_reentry(candidate, _fresh_repo(tmp_path, monkeypatch, "i"))
