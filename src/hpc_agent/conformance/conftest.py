"""Pytest wiring for the harness-conformance kit (K1).

This ``conftest`` is loaded ONLY when the kit is collected
(``pytest --pyargs hpc_agent.conformance``); the core test suite
(``testpaths = ["tests"]``) never sees it, so ``--harness-adapter`` and the
capability fixtures don't leak into core CI. It provides:

* ``--harness-adapter`` loading (``_loader.load_adapter``);
* the ``harness_adapter`` / ``fixture_repo`` fixtures;
* per-capability ``require_*`` fixtures that SKIP a capability's modules WITH
  the contract-named degraded tier when the adapter doesn't declare it;
* the conformance report summary hook (the ``conforming: ...`` verdict line).

The pure machinery lives in the pytest-free sibling modules
(``adapter`` / ``_loader`` / ``fixture_repo`` / ``report``); this file only
binds it to pytest so the kit's logic stays unit-testable in
``tests/conformance_kit/``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hpc_agent.conformance import fixture_repo as _fixture_repo
from hpc_agent.conformance._loader import load_adapter
from hpc_agent.conformance.adapter import (
    CAP_BACKGROUNDING,
    CAP_RELAY_ENFORCEMENT,
    CAP_UTTERANCE_LOG,
    declared_capabilities,
    skip_reason_for,
)
from hpc_agent.conformance.report import ConformanceReport

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from hpc_agent.conformance.adapter import HarnessAdapter

_STATE_KEY = pytest.StashKey["_KitState"]()

# module filename NEEDLE -> capability, for attributing a failing test to the
# capability whose module it lives in (report ``failed`` tally).
_MODULE_CAPABILITY: dict[str, str] = {
    "capability_utterance_log": CAP_UTTERANCE_LOG,
    "capability_relay": CAP_RELAY_ENFORCEMENT,
    "capability_backgrounding": CAP_BACKGROUNDING,
}


class _KitState:
    """Per-session tally the report hook renders from."""

    def __init__(self) -> None:
        self.adapter: HarnessAdapter | None = None
        self.skipped: set[str] = set()
        self.failed: set[str] = set()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--harness-adapter",
        action="store",
        default=None,
        metavar="module.path:factory",
        help="Dotted path to a zero-arg factory returning the HarnessAdapter under test.",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.stash[_STATE_KEY] = _KitState()


@pytest.fixture(scope="session")
def harness_adapter(request: pytest.FixtureRequest) -> HarnessAdapter:
    """The adapter under test, loaded from ``--harness-adapter``.

    A usage error (not a skip) when the option is absent: the kit is a TCK —
    there is nothing to certify without an adapter.
    """
    spec = request.config.getoption("--harness-adapter")
    if not spec:
        raise pytest.UsageError(
            "the conformance kit requires --harness-adapter module.path:factory"
        )
    adapter = load_adapter(spec)
    request.config.stash[_STATE_KEY].adapter = adapter
    return adapter


def _require(request: pytest.FixtureRequest, capability: str) -> None:
    """Skip the calling test WITH the degraded tier when *capability* is undeclared."""
    adapter = request.getfixturevalue("harness_adapter")
    if capability not in declared_capabilities(adapter):
        request.config.stash[_STATE_KEY].skipped.add(capability)
        pytest.skip(skip_reason_for(capability))


@pytest.fixture
def require_utterance_log(request: pytest.FixtureRequest) -> None:
    _require(request, CAP_UTTERANCE_LOG)


@pytest.fixture
def require_relay_enforcement(request: pytest.FixtureRequest) -> None:
    _require(request, CAP_RELAY_ENFORCEMENT)


@pytest.fixture
def require_backgrounding(request: pytest.FixtureRequest) -> None:
    _require(request, CAP_BACKGROUNDING)


@pytest.fixture
def fixture_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A claimed, isolated experiment repo honoring ``HPC_JOURNAL_DIR``.

    Pins the journal home to an isolated ``tmp_path`` subdir UNLESS the caller
    already set ``HPC_JOURNAL_DIR`` (their explicit choice wins) — the same
    redirect-then-claim idiom the core suite uses — then claims the repo's
    namespace so utterance/relay reads land where the writer wrote.
    """
    import os

    if not os.environ.get("HPC_JOURNAL_DIR"):
        monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    return _fixture_repo.claim_fixture_repo(tmp_path / "experiment")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Iterator[None]:
    outcome = yield
    report = outcome.get_result()  # type: ignore[attr-defined]  # pluggy Result
    if report.when != "call" or not report.failed:
        return
    for needle, capability in _MODULE_CAPABILITY.items():
        if needle in report.nodeid:
            item.config.stash[_STATE_KEY].failed.add(capability)
            break


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter, exitstatus: int, config: pytest.Config
) -> None:
    state = config.stash[_STATE_KEY]
    if state.adapter is None:
        return
    report = ConformanceReport(
        adapter_name=getattr(state.adapter, "name", "<unnamed>"),
        declared=declared_capabilities(state.adapter),
        skipped=frozenset(state.skipped),
        failed=frozenset(state.failed),
    )
    terminalreporter.write_sep("=", "harness conformance")
    for line in report.to_lines():
        terminalreporter.write_line(line)
