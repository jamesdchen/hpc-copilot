"""How a code-rendered worker prompt reaches a model — the transport seam.

The orchestrator owns *what* a delegated worker runs (the prompt
rendered by :func:`hpc_agent._kernel.extension.spawn_prompt.render_spawn_parts`).
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

from hpc_agent import errors

# A headless ``claude -p --bare`` worker authenticates ONLY via an API key, a
# gateway bearer token, or cloud-provider credentials in its environment. It
# deliberately does NOT read a Claude Code OAuth/subscription login
# (``~/.claude/.credentials.json`` or ``CLAUDE_CODE_OAUTH_TOKEN``) — ``--bare``
# strips that path along with CLAUDE.md / hooks / MCP / skill discovery. So a
# parent session logged in via OAuth would spawn a worker with no usable
# credential; the orchestrator gates on this before spawning.
_WORKER_CREDENTIAL_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
)

_MISSING_CREDENTIAL_REMEDIATION = (
    "worker authentication unavailable: the headless `claude -p --bare` worker "
    "cannot use a Claude Code OAuth/subscription login. Set ANTHROPIC_API_KEY "
    "(or cloud-provider credentials such as CLAUDE_CODE_USE_BEDROCK / "
    "CLAUDE_CODE_USE_VERTEX) in the environment before running `hpc-agent run`."
)


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
    """Outcome of running a worker: its exit code, stdout, and stderr.

    ``output`` is the worker's stdout (the canonical channel for the
    structured report). ``stderr`` is the captured diagnostic stream —
    surfaced so callers that detect a malformed report can include the
    worker's last words in their error message. Optional for
    backward-compat with test fixtures that construct
    ``InvocationResult(exit_code=..., output=...)`` directly.
    """

    exit_code: int
    output: str
    stderr: str = ""


class WorkerInvoker(Protocol):
    """Runs a fully-rendered worker prompt and returns the result.

    Implementations know nothing about workflows, skills, or the spawn
    contract — only how to get a :class:`RenderedPrompt` to a model and,
    transport permitting, how to cache its prefix.
    """

    name: str

    def invoke(self, prompt: RenderedPrompt, *, cwd: Path) -> InvocationResult: ...

    def missing_credential_remediation(self) -> str | None:
        """Remediation text if the worker would spawn without a usable credential.

        Returned *before* spawning so the orchestrator can fail fast with an
        actionable message instead of letting the worker die with an opaque
        "Not logged in". ``None`` means a usable credential is present.
        """
        ...


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
                # Force the sandbox OFF for the worker regardless of the
                # caller's global setting. The worker's entire job is to
                # SSH / rsync to a cluster — outbound network the bubblewrap
                # sandbox blocks on Linux/macOS, and which native Windows
                # can't sandbox at all (it warns "Commands will run WITHOUT
                # sandboxing" and degrades). A fresh-context worker does not
                # inherit the interactive session's safety posture; passing
                # this inline (argv element, not shell) avoids the warning
                # corrupting the report contract and keeps behaviour
                # deterministic across platforms.
                "--settings",
                '{"sandbox": {"enabled": false}}',
                "--append-system-prompt",
                prompt.cacheable_prefix,
                prompt.variable_suffix,
            ],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return InvocationResult(
            exit_code=proc.returncode,
            output=proc.stdout,
            stderr=getattr(proc, "stderr", None) or "",
        )

    def missing_credential_remediation(self) -> str | None:
        if any(os.environ.get(var) for var in _WORKER_CREDENTIAL_ENV_VARS):
            return None
        return _MISSING_CREDENTIAL_REMEDIATION


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
        raise errors.SpecInvalid(
            f"unknown worker invoker {chosen!r}; registered: {sorted(_INVOKERS)}"
        )
    return factory()
