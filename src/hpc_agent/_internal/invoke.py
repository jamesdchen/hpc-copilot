"""How a code-rendered worker prompt reaches a model — the transport seam.

The orchestrator owns *what* a delegated worker runs (the prompt
rendered by :func:`hpc_agent.atoms.spawn_prompt.render_spawn_parts`).
A :class:`WorkerInvoker` owns only *how* that prompt reaches a model —
a ``claude -p`` child today, an Agent SDK / raw-API call later.

A worker prompt arrives split into a cacheable prefix and a variable
suffix (:class:`RenderedPrompt`). Each invoker decides how to exploit
that split for prompt caching: the default ``claude-cli`` invoker
conveys the prefix as an *appended system prompt*, which Claude Code
caches automatically. A different transport — a raw Messages-API
invoker — would instead mark the prefix block with explicit
``cache_control``. **The split is the general contract; the caching
mechanism is each invoker's private choice, so nothing is locked to
Claude Code.**

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
class RenderedPrompt:
    """A worker prompt split into its cacheable and variable parts.

    ``cacheable_prefix`` is byte-identical across every run of a given
    workflow — scaffold, inlined skill body, return contract — so it is
    the part worth prompt-caching. ``variable_suffix`` is the
    per-invocation context (experiment_dir, fields). The split is what
    lets an invoker place the prefix where its transport caches best.
    """

    cacheable_prefix: str
    variable_suffix: str

    @property
    def joined(self) -> str:
        """The whole prompt as one string — prefix, blank line, suffix."""
        return f"{self.cacheable_prefix}\n\n{self.variable_suffix}"


@dataclass(frozen=True)
class InvocationResult:
    """Outcome of running a worker: its exit code and captured stdout."""

    exit_code: int
    output: str


class WorkerInvoker(Protocol):
    """Runs a fully-rendered worker prompt and returns the result.

    Implementations know nothing about workflows, skills, or the spawn
    contract — only how to get a :class:`RenderedPrompt` to a model and,
    transport permitting, how to cache its prefix.
    """

    name: str

    def invoke(self, prompt: RenderedPrompt, *, cwd: Path) -> InvocationResult: ...


class ClaudeCliInvoker:
    """Runs the worker as a fresh ``claude -p --bare`` child process.

    The cacheable prefix is passed via ``--append-system-prompt`` so it
    joins Claude Code's automatically-cached system prompt; the variable
    suffix is the user prompt. ``--bare`` skips CLAUDE.md / hooks / MCP
    discovery (it does not affect caching) so the worker's context is a
    reproducible minimum.
    """

    name = "claude-cli"

    def __init__(self, *, executable: str = "claude") -> None:
        self._executable = executable

    def invoke(self, prompt: RenderedPrompt, *, cwd: Path) -> InvocationResult:
        proc = subprocess.run(
            [
                self._executable,
                "-p",
                "--bare",
                "--append-system-prompt",
                prompt.cacheable_prefix,
                prompt.variable_suffix,
            ],
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

    A new transport (a raw Messages-API invoker that places explicit
    ``cache_control``, say) is one call to this plus its class — no
    orchestrator change.
    """
    _INVOKERS[name] = factory


def get_invoker(name: str | None = None) -> WorkerInvoker:
    """Resolve a :class:`WorkerInvoker` (see module docstring for precedence)."""
    chosen = name or os.environ.get("HPC_AGENT_INVOKER") or DEFAULT_INVOKER
    factory = _INVOKERS.get(chosen)
    if factory is None:
        raise ValueError(f"unknown worker invoker {chosen!r}; registered: {sorted(_INVOKERS)}")
    return factory()
