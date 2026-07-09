"""Conformance kit K6 — capability 3 (backgrounding / wake).

``docs/internals/harness-contract.md`` capability 3: a conforming harness can
DETACH a long-running block into a worker that survives the turn, and WAKE /
re-invoke the driving agent when the worker reaches a terminal, with the journal
remaining the durable rendezvous the woken driver reads.

One detached-lifecycle assertion (``docs/design/conformance-kit.md``, capability
3): the kit supplies the stub worker (``fixtures/stub_worker.py``); the adapter's
``start_background`` launches it and ``await_wake`` must yield
``woke=True, terminal_seen=True`` within the timeout — i.e. the started work
completed, woke the driver, and the wake observed the worker's TERMINAL record
in the journal namespace. No scheduler, no SSH, no network.

The kit asserts OUTCOMES (:class:`~hpc_agent.conformance.adapter.WakeEvent`),
never mechanisms — a harness that backgrounds via a thread, a subprocess, or a
scheduler certifies through the same seam. ``require_backgrounding`` SKIPS this
module WITH the contract-named degraded tier ("synchronous in-turn execution;
correctness unaffected") when the adapter does not declare capability 3, so an
honestly-partial harness is reported partial, never failed.

Standalone note: driven by the ``harness_adapter`` fixture (``conftest.py``); a
real run supplies ``--harness-adapter``. The mirror unit test
(``tests/conformance_kit/test_capability_backgrounding.py``) drives these exact
functions against a reference adapter so the shipped assertions stay CI-green.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

from hpc_agent.state.utterances import utterances_path

if TYPE_CHECKING:
    from hpc_agent.conformance.adapter import HarnessAdapter

# The kit-supplied stub worker script (shipped as package data — the
# ``conformance/fixtures/**`` glob in ``pyproject.toml``).
_STUB_WORKER = Path(__file__).parent / "fixtures" / "stub_worker.py"

# The terminal-record filename the stub worker writes and a conforming adapter's
# ``await_wake`` reads back. This filename + the repo's journal namespace is the
# rendezvous CONVENTION between the kit's stub and the adapter under test.
STUB_TERMINAL_NAME = "stub_worker.terminal.json"

# Generous headroom: the worker sleeps ~50 ms, but a cold process launch on a
# loaded CI box can take seconds. Well under any real detach deadline.
_WAKE_TIMEOUT_SEC = 30.0


def stub_terminal_path(experiment_dir: Path) -> Path:
    """The rendezvous path for *experiment_dir*: ``<journal namespace>/<name>``.

    The namespace is resolved through the ONE canonical locator
    (``state/utterances.py::utterances_path`` — ``<home>/<repo_hash>/``, honoring
    ``HPC_JOURNAL_DIR``), never a re-derived hash, so the driver and the woken
    reader meet at exactly the same directory the utterance log lives in.
    """
    return utterances_path(Path(experiment_dir)).parent / STUB_TERMINAL_NAME


def stub_worker_argv(experiment_dir: Path) -> list[str]:
    """The argv the adapter's ``start_background`` launches as detached work.

    The driver resolves the rendezvous path in-process and hands the (pure-
    stdlib) worker the fully-resolved target — the worker imports no ``hpc_agent``
    code, so this is the bare detached-process shape a conforming
    ``start_background`` must be able to run.
    """
    return [sys.executable, str(_STUB_WORKER), str(stub_terminal_path(experiment_dir))]


def test_started_work_wakes_and_sees_terminal(
    harness_adapter: HarnessAdapter,
    fixture_repo: Path,
    require_backgrounding: None,  # noqa: ARG001 — skip-with-tier gate (conftest)
) -> None:
    """Started work completes, wakes the driver, and the wake sees the terminal.

    The single detached-lifecycle assertion: ``start_background`` launches the
    stub worker; ``await_wake`` must report BOTH that the driver was re-invoked
    (``woke``) AND that the wake observed the worker's terminal record
    (``terminal_seen``) — the journal was the durable rendezvous.
    """
    handle = harness_adapter.start_background(fixture_repo, stub_worker_argv(fixture_repo))
    wake = harness_adapter.await_wake(handle, _WAKE_TIMEOUT_SEC)
    assert wake.woke, "await_wake did not re-invoke the driver after the worker detached"
    assert wake.terminal_seen, "the wake did not observe the worker's terminal record"
