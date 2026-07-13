"""K6 backgrounding — mirror unit test (green against a reference adapter).

Drives the SHIPPED kit assertion functions
(``hpc_agent.conformance.test_capability_backgrounding``) against a reference
adapter that launches the stub worker as a real detached subprocess, proving the
kit's capability-3 module passes for a conforming harness. Plus the guard-can-
fire direction (a worker that writes NO terminal → ``terminal_seen`` False) and
the honest skip-with-tier posture for an adapter that omits capability 3.

The reference adapter's ``await_wake`` reads the terminal record back through the
canonical journal locator — the rendezvous the woken driver reads — so
``terminal_seen`` reflects the durable journal, not a private handshake.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

from hpc_agent.conformance import test_capability_backgrounding as kit
from hpc_agent.conformance.adapter import (
    CAP_BACKGROUNDING,
    WakeEvent,
    declared_capabilities,
    skip_reason_for,
)
from hpc_agent.conformance.fixture_repo import claim_fixture_repo
from hpc_agent.state.utterances import utterances_path


class _BgHandle(NamedTuple):
    proc: subprocess.Popen[bytes]
    experiment_dir: Path


class _ReferenceBackgroundAdapter:
    """A reference capability-3 provider: launches argv as a detached subprocess
    and reads the terminal record back from the journal namespace."""

    name = "reference-backgrounding"

    def start_background(self, experiment_dir: Path, argv: list[str]) -> _BgHandle:
        proc = subprocess.Popen(argv, cwd=str(experiment_dir), env=os.environ.copy())
        return _BgHandle(proc, Path(experiment_dir))

    def await_wake(self, handle: _BgHandle, timeout_s: float) -> WakeEvent:
        try:
            handle.proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            handle.proc.kill()
            return WakeEvent(False, False)
        terminal = utterances_path(handle.experiment_dir).parent / kit.STUB_TERMINAL_NAME
        return WakeEvent(True, terminal.exists())


class _NoBackgroundAdapter:
    """A capability-1-only harness — no ``start_background`` / ``await_wake``."""

    name = "no-backgrounding"

    def write_utterance(self, experiment_dir: Path, text: str) -> None: ...


def _claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    repo: Path = claim_fixture_repo(tmp_path / "experiment")
    return repo


def test_reference_adapter_passes_shipped_assertion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SHIPPED kit function passes: started work wakes and the wake sees the
    terminal record (``woke=True, terminal_seen=True``)."""
    repo = _claim(tmp_path, monkeypatch)
    kit.test_started_work_wakes_and_sees_terminal(_ReferenceBackgroundAdapter(), repo, None)


def test_stub_worker_writes_terminal_into_journal_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rendezvous is the journal: the worker's terminal lands at
    ``<journal namespace>/<name>`` and carries a terminal state."""
    import json

    repo = _claim(tmp_path, monkeypatch)
    adapter = _ReferenceBackgroundAdapter()
    handle = adapter.start_background(repo, kit.stub_worker_argv(repo))
    wake = adapter.await_wake(handle, 30.0)
    assert wake == WakeEvent(True, True)
    terminal = kit.stub_terminal_path(repo)
    assert terminal.parent == utterances_path(repo).parent  # same namespace as the log
    assert json.loads(terminal.read_text(encoding="utf-8"))["terminal"] is True


def test_terminal_seen_false_when_worker_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard-can-fire: a worker that exits WITHOUT writing a terminal yields
    ``terminal_seen=False`` — so the passing assertion is meaningful."""
    repo = _claim(tmp_path, monkeypatch)
    adapter = _ReferenceBackgroundAdapter()
    # argv that sleeps then exits 0 without touching the rendezvous path.
    argv = [sys.executable, "-c", "import sys,time; time.sleep(0.02); sys.exit(0)"]
    handle = adapter.start_background(repo, argv)
    wake = adapter.await_wake(handle, 30.0)
    assert wake.woke is True
    assert wake.terminal_seen is False


def test_no_backgrounding_adapter_does_not_declare_capability_3() -> None:
    """An adapter omitting the capability-3 methods does not declare it — the
    honest-partial posture (a skip, not a manifest opt-out)."""
    assert CAP_BACKGROUNDING not in declared_capabilities(_NoBackgroundAdapter())


def test_skip_reason_names_the_contract_degraded_tier() -> None:
    """The skip a partial harness earns names the contract tier VERBATIM — the
    ``require_backgrounding`` gate skips with exactly this reason."""
    reason = skip_reason_for(CAP_BACKGROUNDING)
    assert "synchronous in-turn execution; correctness unaffected" in reason


class _FakeState:
    def __init__(self) -> None:
        self.skipped: set[str] = set()
        self.failed: set[str] = set()
        self.adapter: object | None = None


class _FakeStash:
    def __init__(self, state: _FakeState) -> None:
        self._state = state

    def __getitem__(self, _key: object) -> _FakeState:
        return self._state


class _FakeConfig:
    def __init__(self, state: _FakeState) -> None:
        self.stash = _FakeStash(state)


class _FakeRequest:
    def __init__(self, adapter: object, state: _FakeState) -> None:
        self._adapter = adapter
        self.config = _FakeConfig(state)

    def getfixturevalue(self, _name: str) -> object:
        return self._adapter


def test_require_backgrounding_skips_with_tier_when_undeclared() -> None:
    """The real conftest ``require_backgrounding`` gate SKIPS an undeclared
    capability WITH the contract-named degraded tier, and records the skip on the
    report state — exercised through the actual ``_require`` machinery."""
    from hpc_agent.conformance import conftest as kit_conftest

    state = _FakeState()
    request = _FakeRequest(_NoBackgroundAdapter(), state)
    with pytest.raises(pytest.skip.Exception, match="synchronous in-turn execution"):
        kit_conftest._require(request, CAP_BACKGROUNDING)  # type: ignore[arg-type]
    assert CAP_BACKGROUNDING in state.skipped
