"""Capability 5 (Stop-hook append channel) — mirror unit test (core CI).

Drives the SHIPPED kit assertions
(``hpc_agent.conformance.test_capability_stop_hook_append``) against the REFERENCE
relay-audit completer core (green — the behaved-for-the-reference-adapter leg) over
a seeded owed/poisoned journal, AND against planted NON-conforming fakes the kit
correctly FAILS (guard-can-fire):

* a channel that NEVER displays a systemMessage (the rejector degrade) trips shape A;
* a channel that SWALLOWS the systemMessage on a blocked stop trips shape B.

Plus: an adapter that IMPLEMENTS ``run_stop_hook_append`` DECLARES capability 5, and
the reference candidate NEVER leaks the ``HPC_STOP_HOOK_APPEND`` env markers.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hpc_agent.conformance import test_capability_stop_hook_append as kit
from hpc_agent.conformance.adapter import (
    CAP_STOP_HOOK_APPEND,
    StopAppendOutcome,
    declared_capabilities,
)
from hpc_agent.conformance.fixture_repo import claim_fixture_repo


def _fresh_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> Path:
    """A distinct claimed repo, with the append markers scrubbed first.

    Scrubbing before each check is the known local trap the task-level warning
    names: an ambient ``HPC_STOP_HOOK_APPEND`` would make the reference completer
    active regardless of the candidate, so the seam under test must set it ITSELF
    (scoped) — not inherit it.
    """
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.delenv("HPC_STOP_HOOK_APPEND", raising=False)
    monkeypatch.delenv("HPC_STOP_HOOK_APPEND_ON_BLOCK", raising=False)
    repo: Path = claim_fixture_repo(tmp_path / name)
    return repo


# ─── the reference core passes both shipped batteries ────────────────────────


def test_reference_core_displays_on_proceed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kit.check_displays_systemmessage_on_proceed(
        kit._builtin_reference(), _fresh_repo(tmp_path, monkeypatch, "a")
    )


def test_reference_core_displays_with_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kit.check_displays_systemmessage_with_block(
        kit._builtin_reference(), _fresh_repo(tmp_path, monkeypatch, "b")
    )


def test_reference_never_leaks_the_append_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scoped activation is always restored — a sibling probe never inherits it."""
    repo = _fresh_repo(tmp_path, monkeypatch, "c")
    assert "HPC_STOP_HOOK_APPEND" not in os.environ
    kit.check_displays_systemmessage_with_block(kit._builtin_reference(), repo)
    assert "HPC_STOP_HOOK_APPEND" not in os.environ
    assert "HPC_STOP_HOOK_APPEND_ON_BLOCK" not in os.environ


# ─── guard-can-fire: non-conforming fakes are FAILED by the kit ──────────────


def _never_displays() -> kit.StopAppendCandidate:
    """A channel that NEVER appends (the rejector degrade masquerading as active)."""
    return kit.StopAppendCandidate(
        name="fake-never-displays",
        run=lambda _repo, *, final_message, on_block=False: StopAppendOutcome(None, on_block),
    )


def _swallows_on_block() -> kit.StopAppendCandidate:
    """Displays on a proceeding stop but SWALLOWS the systemMessage on a blocked one."""

    def run(repo: Path, *, final_message: str, on_block: bool = False) -> StopAppendOutcome:
        if on_block:
            return StopAppendOutcome(None, True)  # swallowed the blocked-stop message
        return StopAppendOutcome("code-appended verdict", False)

    return kit.StopAppendCandidate(name="fake-swallows-on-block", run=run)


def test_fake_that_never_displays_fails_shape_a(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A channel that never appends FAILS the proceeding-stop display battery."""
    with pytest.raises(AssertionError, match="systemMessage"):
        kit.check_displays_systemmessage_on_proceed(
            _never_displays(), _fresh_repo(tmp_path, monkeypatch, "d")
        )


def test_fake_that_swallows_on_block_fails_shape_b(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A channel that swallows the blocked-stop systemMessage FAILS shape B."""
    with pytest.raises(AssertionError, match="systemMessage"):
        kit.check_displays_systemmessage_with_block(
            _swallows_on_block(), _fresh_repo(tmp_path, monkeypatch, "e")
        )


# ─── the adapter seam: implementing the method DECLARES capability 5 ─────────


class _StopAppendAdapter:
    """A minimal harness declaring ONLY capability 5 (the Wave-D adapter shape)."""

    name = "stop-hook-append-only"

    def run_stop_hook_append(
        self, experiment_dir: Path, *, final_message: str, on_block: bool = False
    ) -> StopAppendOutcome:
        return kit._builtin_reference().run(
            experiment_dir, final_message=final_message, on_block=on_block
        )


def test_adapter_implementing_run_stop_hook_append_declares_capability_5() -> None:
    assert CAP_STOP_HOOK_APPEND in declared_capabilities(_StopAppendAdapter())


def test_adapter_declaring_capability_5_passes_the_battery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A declared capability-5 adapter certifies through the SAME shipped batteries."""
    adapter = _StopAppendAdapter()
    candidate = kit.StopAppendCandidate(name=adapter.name, run=adapter.run_stop_hook_append)
    kit.check_displays_systemmessage_on_proceed(candidate, _fresh_repo(tmp_path, monkeypatch, "f"))
    kit.check_displays_systemmessage_with_block(candidate, _fresh_repo(tmp_path, monkeypatch, "g"))
