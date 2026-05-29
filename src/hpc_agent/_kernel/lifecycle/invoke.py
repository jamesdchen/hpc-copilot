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
environment variable > auto-selection from the ambient credentials
(:func:`_auto_select_invoker`): API-key / cloud-provider creds → the
proven ``--bare`` ``claude-cli`` path; else a Claude Code OAuth
credentials file on a supported OS → ``claude-cli-oauth``; else
``claude-cli`` so its pre-spawn credential guard fires.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
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

# The worker runs a deterministic execution sequence (rsync / qsub / canary),
# not open-ended reasoning, and the spawn scaffold instructs it to escalate
# (needs_resolution) rather than grind on anything it can't resolve. A small,
# cheap model is therefore both sufficient and far cheaper per spawn than the
# caller's interactive model.
_WORKER_MODEL = "haiku"

# OAuth worker auth is unsupported on macOS: the Claude Code OAuth token lives
# in the Keychain there, not a linkable credentials file, so there is nothing to
# relocate into an ephemeral CLAUDE_CONFIG_DIR. Those users keep the API-key
# requirement.
_OAUTH_MACOS_REMEDIATION = (
    "worker authentication unavailable: OAuth worker auth is unsupported on "
    "macOS, where the Claude Code OAuth token lives in the Keychain rather than "
    "a linkable credentials file. Set ANTHROPIC_API_KEY (or cloud-provider "
    "credentials) before running `hpc-agent run` on macOS."
)


def _oauth_credentials_path() -> Path | None:
    """Path to the live Claude Code OAuth credentials file, or ``None`` on macOS.

    ``CLAUDE_CONFIG_DIR`` relocates the user-level config and the OAuth creds
    with it; otherwise the file is ``~/.claude/.credentials.json``
    (``%USERPROFILE%\\.claude\\.credentials.json`` on Windows, which
    ``Path.home()`` resolves). On macOS the token is in the Keychain, not a
    file — there is nothing to link, so this returns ``None`` and OAuth worker
    auth is unsupported there.
    """
    if sys.platform == "darwin":
        return None
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(config_dir) if config_dir else Path.home() / ".claude"
    return base / ".credentials.json"


def _oauth_credentials_available() -> bool:
    """True when a linkable OAuth credentials file exists on a supported OS."""
    path = _oauth_credentials_path()
    return path is not None and path.is_file()


def worker_credentials_available() -> bool:
    """True when a spawned worker would have a usable credential.

    Mirrors the affirmative branches of :func:`_auto_select_invoker`: API-key /
    cloud-provider creds in the environment (→ the ``--bare`` claude-cli worker)
    or a Claude Code OAuth credentials file on a supported OS (→
    ``claude-cli-oauth``). Returns ``False`` only when NEITHER is present — the
    fall-through case where any spawn would fail its pre-spawn credential guard.

    ``hpc-agent run`` uses this to refuse an agent-supplied ``--inline`` when a
    real worker is available: inline trades away the worker's context isolation
    and is a user opt-in, not something an agent should synthesize around an
    (unfounded) worker-auth worry (#155).
    """
    if any(os.environ.get(var) for var in _WORKER_CREDENTIAL_ENV_VARS):
        return True
    return _oauth_credentials_available()


def _link_credentials(live: Path, link: Path) -> None:
    """Point *link* at the live OAuth creds *live* inside the ephemeral dir.

    Symlink rather than copy: a mid-session OAuth token refresh writes back to
    the live file, and the worker must see that refreshed token (and a refresh
    the worker writes through the link must reach the live file). Windows
    symlinks need a privilege the process may lack, so fall back to a hardlink
    (same volume, no privilege) and finally to a copy — which authenticates the
    worker but loses the refresh write-through.
    """
    try:
        os.symlink(live, link)
        return
    except (OSError, NotImplementedError):
        pass
    try:
        os.link(live, link)
    except OSError:
        shutil.copy2(live, link)


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


def _run_claude_worker(
    *,
    executable: str,
    mode_args: list[str],
    prompt: RenderedPrompt,
    cwd: str,
    env: dict[str, str] | None = None,
) -> InvocationResult:
    """Spawn ``claude -p`` with the worker prompt kept OFF the command line.

    Neither half of the prompt rides argv: the cacheable prefix (the rendered
    worker procedure — tens of KB) is written to a temp file and passed via
    ``--append-system-prompt-file``, and the variable suffix is fed on stdin
    (``claude -p`` reads stdin when given no positional prompt). Windows
    ``CreateProcessW`` caps the WHOLE command line at 32,767 characters and the
    rendered submit prompt blows past it (#169); POSIX ``ARG_MAX`` is ~2 MB, but
    the temp-file + stdin transport is identical on every platform, so it is
    taken unconditionally — one code path the test suite exercises everywhere
    rather than a Windows-only branch CI never runs.

    The cacheable-prefix / variable-suffix split is preserved: the prefix is
    still an *appended system prompt* (byte-identical across runs, so Claude
    Code caches it) and the suffix is still the user message — only the
    transport moves off argv, so prompt caching is unaffected.
    """
    with tempfile.TemporaryDirectory(prefix="hpc-agent-worker-prompt-") as prompt_dir:
        system_prompt_file = Path(prompt_dir) / "append_system_prompt.txt"
        system_prompt_file.write_text(prompt.cacheable_prefix, encoding="utf-8")
        proc = subprocess.run(
            [
                executable,
                "-p",
                *mode_args,
                "--append-system-prompt-file",
                str(system_prompt_file),
            ],
            # The variable suffix is the user prompt; feeding it on stdin keeps
            # it off argv alongside the system prompt (see the docstring).
            input=prompt.variable_suffix,
            cwd=cwd,
            env=env,
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


class ClaudeCliInvoker:
    """Runs the worker as a fresh ``claude -p --bare`` child process.

    The cacheable prefix is passed via ``--append-system-prompt-file`` so it
    joins Claude Code's automatically-cached system prompt; the variable
    suffix is fed on stdin as the user prompt. Both are kept off argv so the
    rendered prompt can't overrun Windows' command-length limit (#169).
    ``--bare`` skips CLAUDE.md / hooks / MCP discovery (it does not affect
    caching) so the worker's context is a reproducible minimum.
    """

    name = "claude-cli"

    def __init__(self, *, executable: str = "claude") -> None:
        self._executable = executable

    def invoke(self, prompt: RenderedPrompt, *, cwd: Path) -> InvocationResult:
        return _run_claude_worker(
            executable=self._executable,
            mode_args=[
                "--bare",
                # Pin the worker to a small, cheap model: it executes a
                # deterministic rsync / qsub / canary sequence and is instructed
                # to escalate rather than reason hard, so a large model is wasted
                # spend here.
                "--model",
                _WORKER_MODEL,
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
            ],
            prompt=prompt,
            cwd=str(cwd),
        )

    def missing_credential_remediation(self) -> str | None:
        if any(os.environ.get(var) for var in _WORKER_CREDENTIAL_ENV_VARS):
            return None
        return _MISSING_CREDENTIAL_REMEDIATION


class ClaudeCliOAuthInvoker:
    """Runs the worker as a ``claude -p`` child authenticated by an OAuth login.

    The ``--bare`` worker (:class:`ClaudeCliInvoker`) cannot read a Claude Code
    OAuth/subscription login, so subscription users without an API key have no
    way to run workers. This invoker drops ``--bare`` and instead points
    ``CLAUDE_CONFIG_DIR`` at an ephemeral directory whose only content is the
    live OAuth credentials file (linked in): that yields OAuth auth plus a
    near-bare *user-level* context — no user CLAUDE.md / hooks / MCP / skill
    discovery, because the ephemeral config dir holds none of those.

    A non-``--bare`` ``claude`` still loads project ``.claude/`` from its
    working directory, so the child runs in a clean temp directory rather than
    the experiment dir; the worker takes ``experiment_dir`` from the prompt's
    invocation context (see :func:`render_spawn_parts`), not the cwd. That
    keeps a user repo's ``.claude/`` out of the worker's context and off its
    stdout report.

    Unsupported on macOS (the OAuth token is in the Keychain, not a file);
    :meth:`missing_credential_remediation` returns a message there so the
    pre-spawn guard in ``run_workflow`` fails fast.
    """

    name = "claude-cli-oauth"

    def __init__(self, *, executable: str = "claude") -> None:
        self._executable = executable

    def invoke(self, prompt: RenderedPrompt, *, cwd: Path) -> InvocationResult:
        # ``cwd`` (the experiment dir) is deliberately NOT the child's working
        # directory: a non-``--bare`` ``claude`` loads project ``.claude/`` from
        # its cwd, which would pollute the worker's context and corrupt the
        # stdout report. The worker reads experiment_dir from the prompt itself.
        creds = _oauth_credentials_path()
        if creds is None or not creds.is_file():
            # The pre-spawn guard normally catches this; mirror the remediation
            # as a failed result so a direct caller still gets a clear message
            # rather than an opaque subprocess "Not logged in".
            return InvocationResult(
                exit_code=1, output="", stderr=self.missing_credential_remediation() or ""
            )
        with (
            tempfile.TemporaryDirectory(prefix="hpc-agent-oauth-cfg-") as config_dir,
            tempfile.TemporaryDirectory(prefix="hpc-agent-oauth-cwd-") as clean_cwd,
        ):
            _link_credentials(creds, Path(config_dir) / ".credentials.json")
            return _run_claude_worker(
                executable=self._executable,
                mode_args=[
                    # No ``--bare``: it strips the OAuth-credential path. The
                    # relocated CLAUDE_CONFIG_DIR (below) holding only the linked
                    # creds gives OAuth auth + a near-bare user-level context.
                    "--model",
                    _WORKER_MODEL,
                    # Force the sandbox off for the same reason as the --bare
                    # path: the worker SSH/rsyncs to a cluster (see
                    # ClaudeCliInvoker.invoke).
                    "--settings",
                    '{"sandbox": {"enabled": false}}',
                ],
                prompt=prompt,
                cwd=clean_cwd,
                env={**os.environ, "CLAUDE_CONFIG_DIR": config_dir},
            )

    def missing_credential_remediation(self) -> str | None:
        creds = _oauth_credentials_path()
        if creds is None:  # unsupported OS (macOS Keychain)
            return _OAUTH_MACOS_REMEDIATION
        if creds.is_file():
            return None
        return (
            "worker authentication unavailable: no Claude Code OAuth credentials "
            f"found at {creds}. Log in with `claude` (or `claude setup-token`), "
            "or set ANTHROPIC_API_KEY, before running `hpc-agent run`."
        )


_INVOKERS: dict[str, Callable[..., WorkerInvoker]] = {
    "claude-cli": ClaudeCliInvoker,
    "claude-cli-oauth": ClaudeCliOAuthInvoker,
}
DEFAULT_INVOKER = "claude-cli"


def register_invoker(name: str, factory: Callable[..., WorkerInvoker]) -> None:
    """Register a :class:`WorkerInvoker` factory under *name*.

    A new transport (a raw Messages-API invoker that places explicit
    ``cache_control``, say) is one call to this plus its class — no
    orchestrator change.
    """
    _INVOKERS[name] = factory


def _auto_select_invoker() -> str:
    """Pick a worker invoker from the ambient credential state.

    ``ANTHROPIC_API_KEY`` / cloud-provider creds present → the proven
    ``--bare`` ``claude-cli`` path. Otherwise a Claude Code OAuth credentials
    file on a supported OS → ``claude-cli-oauth``. Otherwise fall back to
    ``claude-cli`` so its pre-spawn credential guard fires with an actionable
    message rather than silently picking a path that cannot authenticate.
    """
    if any(os.environ.get(var) for var in _WORKER_CREDENTIAL_ENV_VARS):
        return "claude-cli"
    if _oauth_credentials_available():
        return "claude-cli-oauth"
    return DEFAULT_INVOKER


def get_invoker(name: str | None = None) -> WorkerInvoker:
    """Resolve a :class:`WorkerInvoker` (see module docstring for precedence)."""
    chosen = name or os.environ.get("HPC_AGENT_INVOKER") or _auto_select_invoker()
    if chosen == "inline":
        # "inline" is a valid HPC_AGENT_INVOKER value but not a spawning
        # transport: it means the caller runs the procedure in its own context.
        # `hpc-agent run` intercepts it before reaching here (see cli/spawn.py);
        # any code path that needs to actually spawn a worker cannot honor it.
        raise errors.SpecInvalid(
            "HPC_AGENT_INVOKER='inline' selects in-context execution, which only "
            "`hpc-agent run` supports; this path requires a spawning transport "
            f"({sorted(_INVOKERS)})."
        )
    factory = _INVOKERS.get(chosen)
    if factory is None:
        raise errors.SpecInvalid(
            f"unknown worker invoker {chosen!r}; registered: {sorted(_INVOKERS)}"
        )
    return factory()
