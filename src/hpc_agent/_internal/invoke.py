"""How a code-rendered worker prompt reaches a model — the transport seam.

The orchestrator owns *what* a delegated worker runs (the prompt
rendered by :func:`hpc_agent.atoms.spawn_prompt.render_spawn_prompt`).
A :class:`WorkerInvoker` owns only *how* that prompt reaches a model —
a ``claude -p`` child today, an Agent SDK call later. Swapping invokers
must never change the prompt or the spawn contract; the interface is
deliberately narrow — a rendered prompt in, an exit code plus captured
stdout out — so an invoker cannot drift from the contract.

Selection precedence: an explicit name > the ``HPC_AGENT_INVOKER``
environment variable > :data:`DEFAULT_INVOKER`.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class InvocationResult:
    """Outcome of running a worker: its exit code and captured stdout."""

    exit_code: int
    output: str


class WorkerInvoker(Protocol):
    """Runs a fully-rendered worker prompt and returns the result.

    Implementations know nothing about workflows, skills, or the spawn
    contract — only how to get a prompt to a model.
    """

    name: str

    def invoke(self, prompt: str, *, cwd: Path) -> InvocationResult: ...


class ClaudeCliInvoker:
    """Runs the worker as a fresh ``claude -p --bare`` child process.

    ``--bare`` skips CLAUDE.md / hooks / MCP discovery, so the worker's
    context is exactly the rendered prompt plus what it reloads from
    disk via ``load-context``.
    """

    name = "claude-cli"

    def __init__(self, *, executable: str = "claude") -> None:
        self._executable = executable

    def invoke(self, prompt: str, *, cwd: Path) -> InvocationResult:
        proc = subprocess.run(
            [self._executable, "-p", "--bare", prompt],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
        return InvocationResult(exit_code=proc.returncode, output=proc.stdout)


_INVOKERS: dict[str, Callable[..., WorkerInvoker]] = {
    "claude-cli": ClaudeCliInvoker,
}
DEFAULT_INVOKER = "claude-cli"


def register_invoker(name: str, factory: Callable[..., WorkerInvoker]) -> None:
    """Register a :class:`WorkerInvoker` factory under *name*.

    A new transport (an Agent SDK invoker, say) is one call to this plus
    its class — no orchestrator change.
    """
    _INVOKERS[name] = factory


def get_invoker(name: str | None = None) -> WorkerInvoker:
    """Resolve a :class:`WorkerInvoker` (see module docstring for precedence)."""
    chosen = name or os.environ.get("HPC_AGENT_INVOKER") or DEFAULT_INVOKER
    factory = _INVOKERS.get(chosen)
    if factory is None:
        raise ValueError(f"unknown worker invoker {chosen!r}; registered: {sorted(_INVOKERS)}")
    return factory()
