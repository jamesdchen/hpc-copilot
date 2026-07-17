"""The foreign-backgrounding reference adapter (Wave C / T8) — capability 3, no Claude machinery.

Capability 3 (backgrounding / wake) proven by a NON-Claude detach/wake shape
(``docs/internals/harness-contract.md`` capability 3; the plan's Wave-C T8): a
plain OS subprocess detached to survive the turn, an OS-level wait to WAKE the
driver, and the JOURNAL namespace as the durable rendezvous the woken driver
reads. No Stop hook, no ``claude -p`` worker, no watchdog — just
``subprocess.Popen`` + ``wait`` + a terminal record read back through the ONE
canonical journal locator. It proves backgrounding is not Claude-Code-shaped: any
harness that can launch a process and wait for it satisfies capability 3.

* :meth:`ForeignBackgroundingAdapter.start_background` launches the kit's stub
  worker as a bare detached subprocess (the worker imports no hpc-agent code — the
  driver hands it the fully-resolved rendezvous path).
* :meth:`ForeignBackgroundingAdapter.await_wake` waits at the OS level, then reads
  the worker's terminal record back through
  :func:`hpc_agent.state.utterances.utterances_path` — the SAME journal namespace
  the utterance log lives in, so ``terminal_seen`` reflects the durable journal,
  never a private handshake.

It declares NOTHING else: no utterance log, no relay enforcement — the kit SKIPS
those with their contract-named degraded tiers and the report reads
``partial: backgrounding``. Partial is honest, not a failure.

Stdlib + hpc-agent PUBLIC surface only; pytest-free (the D-K1 boundary). Loadable
via ``--harness-adapter hpc_agent.conformance.adapters.foreign_backgrounding:build``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import NamedTuple

from hpc_agent.conformance.adapter import CAP_BACKGROUNDING, WakeEvent

__all__ = ["ForeignBackgroundingAdapter", "build"]

# The terminal-record filename the kit's stub worker writes at the rendezvous path
# and this adapter's ``await_wake`` reads back — the kit's rendezvous CONVENTION.
_STUB_TERMINAL_NAME = "stub_worker.terminal.json"


class _ForeignHandle(NamedTuple):
    """The handle ``start_background`` returns to ``await_wake`` — a bare process."""

    proc: subprocess.Popen[bytes]
    experiment_dir: Path


class ForeignBackgroundingAdapter:
    """A plain-subprocess detach/wake harness behind the kit's adapter seam."""

    name = "foreign-backgrounding"

    def start_background(self, experiment_dir: Path, argv: list[str]) -> _ForeignHandle:
        """Detach *argv* as a bare OS subprocess that survives the turn.

        The non-Claude detach shape: no worker binary, no hook — just
        ``subprocess.Popen``. The worker (the kit's stub) meets the driver only at
        the journal-namespace rendezvous the driver resolved and passed in argv.
        """
        proc = subprocess.Popen(argv, cwd=str(experiment_dir), env=os.environ.copy())
        return _ForeignHandle(proc, Path(experiment_dir))

    def await_wake(self, handle: _ForeignHandle, timeout_s: float) -> WakeEvent:
        """WAKE via an OS-level wait, then read the terminal record from the journal.

        ``woke`` is True once the detached process returns within the timeout;
        ``terminal_seen`` reads the rendezvous file through the ONE canonical
        journal locator, so it reflects the durable journal — the woken driver's
        resume anchor — not a private channel. A timeout kills the process and
        reports neither (the honest no-wake).
        """
        from hpc_agent.state.utterances import utterances_path

        try:
            handle.proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            handle.proc.kill()
            return WakeEvent(woke=False, terminal_seen=False)
        terminal = utterances_path(handle.experiment_dir).parent / _STUB_TERMINAL_NAME
        return WakeEvent(woke=True, terminal_seen=terminal.exists())

    def detect_capabilities(self, experiment_dir: Path) -> frozenset[str]:  # noqa: ARG002
        """Detect ``backgrounding`` — the core-side constant, honest for any harness.

        Backgrounding detection is a core-side constant (the detached-worker path is
        core), so it is NOT a per-harness negotiation SEAM: the kit asserts only its
        BEHAVED leg. This harness reports it present (it provides the detach/wake it
        behaves) and claims no seam capability — utterance-log and relay-enforcement
        are genuinely absent.
        """
        return frozenset({CAP_BACKGROUNDING})


def build() -> ForeignBackgroundingAdapter:
    """Zero-arg factory for ``--harness-adapter …adapters.foreign_backgrounding:build``."""
    return ForeignBackgroundingAdapter()
