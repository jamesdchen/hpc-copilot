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
credentials file on a supported OS → ``claude-cli-oauth``; else, when no
Claude credential is present, a ``CODEX_API_KEY`` → ``codex-cli`` or a
``GEMINI_API_KEY`` / ``GOOGLE_API_KEY`` → ``gemini-cli``; else
``claude-cli`` so its pre-spawn credential guard fires.

The :class:`WorkerInvoker` contract — what every driver must normalize
=====================================================================

The Protocol has **two methods**: :meth:`~WorkerInvoker.invoke` and
:meth:`~WorkerInvoker.missing_credential_remediation` (the pre-spawn
auth guard ``run_workflow`` calls *before* spawning, so a missing
credential fails fast with actionable text rather than an opaque "Not
logged in"). A new harness driver normalizes the same four axes the
proven ``claude-cli`` driver does — each on its harness's own config
surface, with its own precedence rules:

1. **Headless transport.** Get the :class:`RenderedPrompt` to the model
   non-interactively, keeping the (tens-of-KB) prompt OFF argv so it
   can't overrun Windows' 32 KB command-line limit (#169). Claude:
   ``claude -p`` with the prefix in ``--append-system-prompt-file`` and
   the suffix on stdin. Codex: ``codex exec`` with the whole prompt on
   stdin, final report read back from ``--output-last-message``. Gemini:
   ``gemini -p`` with the prefix as a full-replacement system prompt via
   ``GEMINI_SYSTEM_MD`` and the suffix on stdin, output unwrapped from
   the ``--output-format json`` envelope.
2. **Sandbox / network posture.** The worker SSHes/rsyncs out
   unattended, which a sandbox blocks — so each driver forces the
   sandbox OFF. Claude: ``--settings '{"sandbox":{"enabled":false}}'``.
   Codex: ``--dangerously-bypass-approvals-and-sandbox`` (it defaults to
   read-only). Gemini: leave ``GEMINI_SANDBOX`` unset (it names a
   backend, not a bool — omitting it selects none).
3. **Tool-authorization fence.** Full network/disk access is NOT
   unfenced: every driver re-installs the no-``scancel``/no-exfil deny
   (:data:`_CLUSTER_OP_DENY_COMMANDS`, the #283/#228 invariant) on its
   own config surface. Claude: ``--allowedTools`` / ``--disallowedTools``
   (deny beats allow). Codex: an ``execpolicy`` ``.rules`` file
   (``decision="forbidden"``, strictest-severity-wins → deny overrides
   allow). Gemini: a Policy Engine TOML installed at the **User/Admin**
   tier (NOT workspace tier — #18186 silently no-ops it), with deny
   entries out-ranking allow by ``priority``.
4. **Decode-schema-vs-floor.** :func:`parse_worker_report` is the always
   -on floor that validates the worker's final JSON report. A driver MAY
   add a decode-time schema accelerator on top, but it is optional, off by
   default, and gated PER HARNESS (turning it on is gated on a per-harness
   live-validation run, #269). Claude: ``--json-schema`` gated by
   ``HPC_AGENT_WORKER_JSON_SCHEMA`` (lenient ``worker.output.json``).
   Codex: ``--output-schema`` gated by ``HPC_AGENT_CODEX_OUTPUT_SCHEMA``
   (API-strict ``worker.strict.output.json``). Gemini: no CLI decode
   schema exists (``responseSchema`` is API/SDK-only), so the Gemini path
   leans entirely on the #304 floor.

Plus :attr:`InvocationResult.cache_stats`: populated only when the
caller asks (``report_cache_stats=True``) AND the transport surfaces
billing usage (Claude's ``--output-format json`` usage block). Codex and
Gemini do not expose a cache-creation/cache-read split at the CLI layer,
so their ``cache_stats`` is ``None``.
"""

from __future__ import annotations

import json
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

# Codex worker auth. ``CODEX_API_KEY`` is scoped to the invocation and is
# preferred over ambient ``OPENAI_API_KEY``: a stored ChatGPT login in
# ``~/.codex/auth.json`` can shadow ``OPENAI_API_KEY`` (#3286), so relying on
# the ambient key is fragile. The driver requires ``CODEX_API_KEY`` and maps it
# onto ``OPENAI_API_KEY`` in the child's environment (the var Codex actually
# reads), so the scoped key authenticates the worker and out-ranks any ambient
# key or stored ChatGPT login.
_CODEX_CREDENTIAL_ENV_VAR = "CODEX_API_KEY"
_CODEX_MISSING_CREDENTIAL_REMEDIATION = (
    "worker authentication unavailable: the headless `codex exec` worker needs "
    "an API key scoped to the invocation. Set CODEX_API_KEY in the environment "
    "before running `hpc-agent run` (preferred over ambient OPENAI_API_KEY, "
    "which a stored ChatGPT login in ~/.codex/auth.json can shadow)."
)

# Gemini worker auth: ``GEMINI_API_KEY`` (Gemini API) or ``GOOGLE_API_KEY``
# (Vertex AI). Either is sufficient; the CLI reads them from the environment.
_GEMINI_CREDENTIAL_ENV_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
_GEMINI_MISSING_CREDENTIAL_REMEDIATION = (
    "worker authentication unavailable: the headless `gemini -p` worker needs "
    "GEMINI_API_KEY (Gemini API) or GOOGLE_API_KEY (Vertex AI) in the "
    "environment before running `hpc-agent run`."
)

# The worker runs a deterministic execution sequence (rsync / qsub / canary),
# not open-ended reasoning, and the spawn scaffold instructs it to escalate
# (needs_resolution) rather than grind on anything it can't resolve. A small,
# cheap model is therefore both sufficient and far cheaper per spawn than the
# caller's interactive model.
_WORKER_MODEL = "haiku"

# Per-harness cheap-model pins (same rationale as ``_WORKER_MODEL``: the worker
# runs a deterministic sequence, so the small model is sufficient). Each is the
# default for its driver and is overridable by an env var for the rare case a
# pinned id is retired upstream before the constant is bumped here. Concrete ids
# only — NOT the ``auto`` / ``flash`` / ``pro`` aliases, which now resolve to a
# preview generation and would silently change the worker's model.
_CODEX_WORKER_MODEL = "gpt-5.4-mini"
_CODEX_WORKER_MODEL_ENV = "HPC_AGENT_CODEX_WORKER_MODEL"
_GEMINI_WORKER_MODEL = "gemini-2.5-flash"
_GEMINI_WORKER_MODEL_ENV = "HPC_AGENT_GEMINI_WORKER_MODEL"


def _worker_model(env_var: str, default: str) -> str:
    """The worker model id: an env override if set non-empty, else *default*."""
    return os.environ.get(env_var, "").strip() or default


# Worker tool fence. BOTH spawn paths bypass settings.json (`--bare` skips it;
# the OAuth path runs from an ephemeral CLAUDE_CONFIG_DIR with no project
# `.claude/`), so the worker — the one surface that actually reaches a cluster —
# is the only place the project's deny never lands. We fence it on the spawn.
#
# The worker procedures now reach the cluster ONLY through `hpc-agent` (which
# does its own ssh+rsync internally, as subprocesses of the binary that a
# Bash-*tool* fence does not touch). Every freestyle `python`/`json.load`/`grep`
# step was removed: file reads use the Read tool, searches use Grep/Glob, run
# discovery uses `hpc-agent discover-runs`, and the dispatcher copy folded into
# `build-tasks-py`. So the worker's entire Bash surface is `hpc-agent` (verbs)
# plus `git` (commit the scaffolded tasks.py/cli.py; never pushes).
#
# ``_WORKER_ALLOWED_TOOLS`` is the strict default-deny allowlist: only those two
# Bash families plus the read/write/search tools. ``_WORKER_DISALLOWED_TOOLS``
# is kept as belt-and-suspenders — in Claude Code a disallow beats an allow, so
# direct cluster transport / scheduler / exfil commands stay blocked even if a
# future allow rule widens. (Runtime enforcement is the CLI's; the tests here
# assert the spawn argv carries both.)
_WORKER_ALLOWED_TOOLS = "Bash(hpc-agent:*) Bash(git:*) Read Write Edit Grep Glob"
_WORKER_DISALLOWED_TOOLS = (
    "Bash(scancel:*) Bash(qdel:*) Bash(qmod:*) Bash(qsub:*) Bash(sbatch:*) "
    "Bash(ssh:*) Bash(rsync:*) Bash(scp:*) Bash(curl:*) Bash(wget:*)"
)

# The bare command names of the cluster-op deny that the Claude fence above
# expresses as ``Bash(<cmd>:*)`` disallow entries. The Codex execpolicy and
# Gemini Policy-Engine fences (which match on a command prefix, not a Claude
# tool pattern) deny the SAME surface, so they derive from one canonical tuple
# rather than re-typing it three times. This is the #283/#228 no-scancel /
# no-exfil invariant: scheduler cancel/submit (``scancel`` / ``qdel`` /
# ``bkill`` / ``qsub`` / ``sbatch`` / ``qmod``), raw cluster transport (``ssh``
# / ``rsync`` / ``scp`` — the worker reaches the cluster only through
# ``hpc-agent``, which does its own transport internally), and exfil (``curl``
# / ``wget``). ``bkill`` (LSF cancel) is included alongside ``scancel`` (Slurm)
# and ``qdel`` (PBS/SGE) so the cancel deny covers every scheduler family.
_CLUSTER_OP_DENY_COMMANDS = (
    "scancel",
    "qdel",
    "bkill",
    "qmod",
    "qsub",
    "sbatch",
    "ssh",
    "rsync",
    "scp",
    "curl",
    "wget",
)

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

    ``cache_stats`` is the worker's prompt-cache token accounting (#244) —
    ``{"input_tokens", "output_tokens", "cache_creation_input_tokens",
    "cache_read_input_tokens"}`` (whichever the transport reported) — populated
    only when the caller asked for it (``report_cache_stats=True``) and the
    invoker's transport surfaces billing usage. ``None`` otherwise: not
    requested, or the transport doesn't expose it.
    """

    exit_code: int
    output: str
    stderr: str = ""
    cache_stats: dict[str, int] | None = None


class WorkerInvoker(Protocol):
    """Runs a fully-rendered worker prompt and returns the result.

    Implementations know nothing about workflows, skills, or the spawn
    contract — only how to get a :class:`RenderedPrompt` to a model and,
    transport permitting, how to cache its prefix.
    """

    name: str

    def invoke(
        self, prompt: RenderedPrompt, *, cwd: Path, report_cache_stats: bool = False
    ) -> InvocationResult: ...

    def missing_credential_remediation(self) -> str | None:
        """Remediation text if the worker would spawn without a usable credential.

        Returned *before* spawning so the orchestrator can fail fast with an
        actionable message instead of letting the worker die with an opaque
        "Not logged in". ``None`` means a usable credential is present.
        """
        ...


_CACHE_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _extract_cache_stats(envelope: dict[str, object]) -> dict[str, int] | None:
    """Pull the prompt-cache token counts out of a ``--output-format json`` envelope.

    Claude Code's JSON result envelope carries a ``usage`` block with the
    Anthropic billing token counts — including ``cache_read_input_tokens`` and
    ``cache_creation_input_tokens``, the two fields that reveal whether the
    cacheable worker-prompt prefix actually hit cache (#244). Returns the
    integer-valued subset of :data:`_CACHE_USAGE_KEYS`, or ``None`` when no
    usage block is present.
    """
    usage = envelope.get("usage")
    if not isinstance(usage, dict):
        return None
    stats = {k: int(usage[k]) for k in _CACHE_USAGE_KEYS if isinstance(usage.get(k), int)}
    return stats or None


# Decode-time output constraint. When enabled, the worker is spawned with the
# harness's decode-schema flag so the CLI constrains the agent's FINAL report
# to the WorkerReport schema at decode time — the agent still runs its rsync /
# qsub / canary tool loop; only the terminal message is schema-forced. This is
# the *structural* half of the contract; ``parse_worker_report`` still enforces
# the cross-field invariants a schema cannot express (a non-empty ``why`` at
# judgement points), so the two are complementary, not substitutes.
#
# The gate is split PER HARNESS, because turning the accelerator on is gated on
# a LIVE validation run and that validation is per-harness: "does the
# decode-schema compose with the agent loop" and "does this CLI accept this
# schema shape" are separate empirical questions for each CLI (different flags,
# different strictness requirements). A single shared gate would flip an
# unvalidated harness on as a side effect of validating another.
#
#   * Claude ``--json-schema`` → ``HPC_AGENT_WORKER_JSON_SCHEMA``. Emits the
#     *lenient* ``worker.output.json``: whether claude's mode requires a strict
#     schema is the open #269 question, unanswerable offline, so the lenient
#     shape stays until the live ``claude -p --json-schema`` run confirms.
#   * Codex ``--output-schema`` → ``HPC_AGENT_CODEX_OUTPUT_SCHEMA``. Emits the
#     *API-strict* ``worker.strict.output.json``: Codex's ``--output-schema``
#     documents that it requires the strict shape (``additionalProperties:false``
#     + all-required), so binding the lenient floor schema was a latent bug.
#
# Both are OFF by default; the plain text transport is otherwise untouched.
# Making either the default once live-validated is tracked in issue #269.
_WORKER_JSON_SCHEMA_ENV = "HPC_AGENT_WORKER_JSON_SCHEMA"
_CODEX_OUTPUT_SCHEMA_ENV = "HPC_AGENT_CODEX_OUTPUT_SCHEMA"


def _decode_schema_enabled(env_var: str) -> bool:
    """Whether the decode-schema gate named by *env_var* is turned on."""
    return os.environ.get(env_var, "").strip().lower() in {"1", "true", "yes", "on"}


def _load_schema_resource(resource: str) -> str | None:
    """Return a packaged ``hpc_agent.schemas`` JSON file minified, or ``None``.

    ``None`` when the resource can't be read — the caller then runs on the
    plain transport and report validation falls back to
    :func:`parse_worker_report` alone.
    """
    try:
        from importlib.resources import files as _files

        text = (_files("hpc_agent.schemas") / resource).read_text(encoding="utf-8")
        return json.dumps(json.loads(text), separators=(",", ":"))
    except (FileNotFoundError, ModuleNotFoundError, OSError, json.JSONDecodeError):
        return None


def _worker_output_schema() -> str | None:
    """The lenient WorkerReport schema (minified) for Claude ``--json-schema``.

    ``None`` when ``HPC_AGENT_WORKER_JSON_SCHEMA`` is off (the default).
    """
    if not _decode_schema_enabled(_WORKER_JSON_SCHEMA_ENV):
        return None
    return _load_schema_resource("worker.output.json")


def _codex_output_schema() -> str | None:
    """The API-strict WorkerReport schema (minified) for Codex ``--output-schema``.

    ``None`` when ``HPC_AGENT_CODEX_OUTPUT_SCHEMA`` is off (the default). Unlike
    Claude's gate this emits ``worker.strict.output.json`` — Codex's
    ``--output-schema`` requires ``additionalProperties:false`` + all-required.
    """
    if not _decode_schema_enabled(_CODEX_OUTPUT_SCHEMA_ENV):
        return None
    return _load_schema_resource("worker.strict.output.json")


def _run_claude_worker(
    *,
    executable: str,
    mode_args: list[str],
    prompt: RenderedPrompt,
    cwd: str,
    env: dict[str, str] | None = None,
    report_cache_stats: bool = False,
    output_schema: str | None = None,
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

    When *report_cache_stats* is set, the child is run with
    ``--output-format json`` so Claude Code wraps its reply in a result
    envelope carrying a ``usage`` block; we lift the worker's report text back
    out of ``result`` (so the report contract is unchanged for the caller) and
    surface the cache token counts on :attr:`InvocationResult.cache_stats`.
    Off by default — the plain text transport is untouched.
    """
    # ``--json-schema`` (decode-time constraint) requires the JSON result
    # envelope, as does cache-stats reporting; either turns on ``--output-format
    # json`` and the envelope-unwrap below.
    use_json = report_cache_stats or output_schema is not None
    json_args = ["--output-format", "json"] if use_json else []
    schema_args = ["--json-schema", output_schema] if output_schema is not None else []
    with tempfile.TemporaryDirectory(prefix="hpc-agent-worker-prompt-") as prompt_dir:
        system_prompt_file = Path(prompt_dir) / "append_system_prompt.txt"
        system_prompt_file.write_text(prompt.cacheable_prefix, encoding="utf-8")
        proc = subprocess.run(
            [
                executable,
                "-p",
                *mode_args,
                *json_args,
                *schema_args,
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
    output = proc.stdout
    cache_stats: dict[str, int] | None = None
    if use_json:
        # Unwrap the JSON result envelope: the worker's report is the inner
        # ``result`` (a string on the plain path; a structured object when
        # ``--json-schema`` constrained it — re-serialize so the caller's
        # parse_worker_report sees a JSON object either way), and ``usage``
        # carries the cache token counts. A malformed/non-JSON stdout (a crash
        # before the envelope) leaves the raw stdout as the output so the
        # caller's report-parse still surfaces the worker's last words;
        # cache_stats just stays None.
        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError:
            envelope = None
        if isinstance(envelope, dict):
            if report_cache_stats:
                cache_stats = _extract_cache_stats(envelope)
            result = envelope.get("result")
            if isinstance(result, dict):
                output = json.dumps(result)
            elif isinstance(result, str):
                output = result
    return InvocationResult(
        exit_code=proc.returncode,
        output=output,
        stderr=getattr(proc, "stderr", None) or "",
        cache_stats=cache_stats,
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

    def invoke(
        self, prompt: RenderedPrompt, *, cwd: Path, report_cache_stats: bool = False
    ) -> InvocationResult:
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
                # Strict tool fence the `--bare` worker would otherwise lack
                # (see _WORKER_ALLOWED_TOOLS / _WORKER_DISALLOWED_TOOLS).
                "--allowedTools",
                _WORKER_ALLOWED_TOOLS,
                "--disallowedTools",
                _WORKER_DISALLOWED_TOOLS,
            ],
            prompt=prompt,
            cwd=str(cwd),
            report_cache_stats=report_cache_stats,
            output_schema=_worker_output_schema(),
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

    def invoke(
        self, prompt: RenderedPrompt, *, cwd: Path, report_cache_stats: bool = False
    ) -> InvocationResult:
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
                    # Same strict fence as the --bare path: the ephemeral
                    # CLAUDE_CONFIG_DIR carries no project settings.json, so
                    # apply the allow/deny on the spawn (see
                    # _WORKER_ALLOWED_TOOLS / _WORKER_DISALLOWED_TOOLS).
                    "--allowedTools",
                    _WORKER_ALLOWED_TOOLS,
                    "--disallowedTools",
                    _WORKER_DISALLOWED_TOOLS,
                ],
                prompt=prompt,
                cwd=clean_cwd,
                env={**os.environ, "CLAUDE_CONFIG_DIR": config_dir},
                report_cache_stats=report_cache_stats,
                output_schema=_worker_output_schema(),
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


def _codex_execpolicy_rules() -> str:
    """The Codex ``execpolicy`` ``.rules`` body fencing the cluster-op deny.

    Starlark ``prefix_rule`` entries with ``decision="forbidden"``: Codex's
    execpolicy resolves overlapping rules by strictest-severity-wins, so a
    ``forbidden`` rule overrides any allow exactly like Claude's deny-beats-allow
    (the third axis). One rule per command in :data:`_CLUSTER_OP_DENY_COMMANDS`
    so the bare invocation (``ssh …``, ``scancel …``) is denied; the network
    deny is covered by the same list (``curl`` / ``wget`` / ``ssh`` / ``rsync``
    / ``scp``). The worker still reaches the cluster through ``hpc-agent``,
    which does its own transport internally as a child of the binary — not a
    top-level command this prefix fence matches.
    """
    lines = [
        "# hpc-agent worker cluster-op fence (execpolicy). Deny scheduler",
        "# cancel/submit, raw cluster transport, and exfil — the #283/#228",
        "# invariant. forbidden wins over any allow (strictest-severity-wins).",
    ]
    for cmd in _CLUSTER_OP_DENY_COMMANDS:
        lines.append(f'prefix_rule(pattern=["{cmd}"], decision="forbidden")')
    return "\n".join(lines) + "\n"


class CodexCliInvoker:
    """Runs the worker as a fresh ``codex exec`` child process.

    Transport (axis 1): the whole rendered prompt (prefix + suffix joined) is
    fed on stdin — Codex has no append-system-prompt cache, so there is no
    prefix/suffix split to exploit at the CLI layer, and nothing rides argv
    (#169). The worker's final report is read back from the file Codex writes
    via ``--output-last-message`` rather than scraping stdout.

    Sandbox (axis 2): ``--dangerously-bypass-approvals-and-sandbox`` — Codex
    defaults to ``read-only`` with approval prompts, but the worker must
    SSH/rsync out unattended, so full disk+network access with zero prompts is
    required. Full access is NOT unfenced: the execpolicy below re-imposes the
    cluster-op deny.

    Tool fence (axis 3): an ``execpolicy`` ``.rules`` file
    (:func:`_codex_execpolicy_rules`) denying the cluster-op surface, pointed at
    via ``--config execpolicy_file=<path>``. ``forbidden`` overrides allow
    (strictest-severity-wins), analogous to Claude's deny-beats-allow.

    Decode schema (axis 4): OPTIONAL ``--output-schema`` gated behind
    ``HPC_AGENT_CODEX_OUTPUT_SCHEMA`` (its own gate, split from Claude's
    ``--json-schema`` so each harness flips only on its own live validation,
    #269) — OFF by default, so the floor :func:`parse_worker_report` is the
    decode path. When on it binds the API-strict ``worker.strict.output.json``.
    ``cache_stats`` is always ``None`` (not surfaced at the CLI).
    """

    name = "codex-cli"

    def __init__(self, *, executable: str = "codex") -> None:
        self._executable = executable

    def invoke(
        self, prompt: RenderedPrompt, *, cwd: Path, report_cache_stats: bool = False
    ) -> InvocationResult:
        # report_cache_stats is accepted for Protocol conformance but Codex does
        # not surface a cache-creation/cache-read split at the CLI layer, so
        # cache_stats stays None regardless.
        env = {**os.environ}
        # Codex authenticates from ``OPENAI_API_KEY`` (or a stored ChatGPT login
        # in ``~/.codex/auth.json``) — it does NOT read ``CODEX_API_KEY``, which
        # is an hpc-agent-side name for the invocation-scoped key. Map it onto
        # ``OPENAI_API_KEY`` in the child so the scoped key actually authenticates
        # the worker AND out-ranks any ambient ``OPENAI_API_KEY`` / stored
        # ChatGPT login that would otherwise shadow it (#3286). Without this the
        # auth guard passes (``CODEX_API_KEY`` present) yet Codex silently falls
        # back to the very credential the guard exists to bypass.
        codex_key = os.environ.get(_CODEX_CREDENTIAL_ENV_VAR)
        if codex_key:
            env["OPENAI_API_KEY"] = codex_key
        with tempfile.TemporaryDirectory(prefix="hpc-agent-codex-") as work_dir:
            rules_file = Path(work_dir) / "cluster_ops.rules"
            rules_file.write_text(_codex_execpolicy_rules(), encoding="utf-8")
            last_message_file = Path(work_dir) / "last_message.txt"
            schema_args = self._output_schema_args(work_dir)
            proc = subprocess.run(
                [
                    self._executable,
                    "exec",
                    "-m",
                    _worker_model(_CODEX_WORKER_MODEL_ENV, _CODEX_WORKER_MODEL),
                    # Full disk+network, zero approval prompts: the worker
                    # SSH/rsyncs to a cluster unattended (see class docstring).
                    # Fenced by the execpolicy below, not left unfenced.
                    "--dangerously-bypass-approvals-and-sandbox",
                    # Re-impose the cluster-op deny on top of full access.
                    "--config",
                    f"execpolicy_file={rules_file}",
                    *schema_args,
                    # The worker's final report goes to this file, read back
                    # below — not scraped from stdout.
                    "--output-last-message",
                    str(last_message_file),
                    # The whole prompt on stdin keeps it off argv (#169).
                    "-",
                ],
                input=prompt.joined,
                cwd=str(cwd),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            # The report is the last-message file Codex wrote; if the worker
            # crashed before writing it, fall back to stdout so the caller's
            # report-parse still surfaces the worker's last words.
            try:
                output = last_message_file.read_text(encoding="utf-8")
            except OSError:
                output = proc.stdout
        return InvocationResult(
            exit_code=proc.returncode,
            output=output,
            stderr=getattr(proc, "stderr", None) or "",
            cache_stats=None,
        )

    @staticmethod
    def _output_schema_args(work_dir: str) -> list[str]:
        """``--output-schema <file>`` when ``HPC_AGENT_CODEX_OUTPUT_SCHEMA`` is on.

        OFF by default, so the floor ``parse_worker_report`` is the decode path.
        When on, Codex's ``--output-schema`` takes a JSON-Schema FILE in the
        API-strict shape (``additionalProperties:false`` + all props
        ``required``); :func:`_codex_output_schema` supplies the checked-in
        ``worker.strict.output.json`` (generated from ``WorkerReport`` by
        ``scripts/build_schemas.py``). The floor is the safety net either way.
        """
        schema = _codex_output_schema()
        if schema is None:
            return []
        schema_file = Path(work_dir) / "worker_output.schema.json"
        schema_file.write_text(schema, encoding="utf-8")
        return ["--output-schema", str(schema_file)]

    def missing_credential_remediation(self) -> str | None:
        if os.environ.get(_CODEX_CREDENTIAL_ENV_VAR):
            return None
        return _CODEX_MISSING_CREDENTIAL_REMEDIATION


# Gemini Policy Engine: a deny only beats an allow when it carries a HIGHER
# ``priority`` (unlike Claude/Codex where deny inherently wins). 1000 is well
# above any plausible allow tier so the cluster-op deny always out-ranks.
_GEMINI_DENY_PRIORITY = 1000


def _gemini_policy_dir() -> Path:
    """The USER-tier Gemini Policy Engine directory (``~/.gemini/policies``).

    The policy fence MUST live at the User (or Admin) tier: the workspace tier
    is currently broken (upstream #18186) — project-local policies silently
    no-op, so a fence installed there would quietly drop the cluster-safety
    invariant. ``GEMINI_DIR`` relocates the Gemini home (mirrors how the OAuth
    Claude driver relocates ``CLAUDE_CONFIG_DIR``); otherwise it is
    ``~/.gemini``. Returned as a directory so the caller can write the TOML.
    """
    base = os.environ.get("GEMINI_DIR")
    root = Path(base) if base else Path.home() / ".gemini"
    return root / "policies"


def _gemini_policy_toml() -> str:
    """The User/Admin-tier Policy Engine TOML fencing the cluster-op deny.

    One ``[[rules]]`` entry per command in :data:`_CLUSTER_OP_DENY_COMMANDS`,
    each ``decision = "deny"`` at :data:`_GEMINI_DENY_PRIORITY` — higher than any
    allow tier, because the Gemini Policy Engine is priority-ordered, not
    deny-beats-allow. ``commandPrefix`` matches the bare invocation
    (``ssh …`` / ``scancel …``); the same list covers the network deny
    (``curl`` / ``wget`` / ``ssh`` / ``rsync`` / ``scp``). The worker still
    reaches the cluster through ``hpc-agent`` (its own transport internally),
    which this top-level prefix fence does not match.
    """
    lines = [
        "# hpc-agent worker cluster-op fence (Gemini Policy Engine, USER tier).",
        "# Installed at the User/Admin tier, NOT workspace — the workspace tier",
        "# silently no-ops (upstream #18186). Deny out-ranks allow via priority",
        "# (the Policy Engine is priority-ordered, not deny-beats-allow). This is",
        "# the #283/#228 no-scancel / no-exfil invariant.",
    ]
    for cmd in _CLUSTER_OP_DENY_COMMANDS:
        lines.append("")
        lines.append("[[rules]]")
        lines.append('toolName = "run_shell_command"')
        lines.append(f'commandPrefix = "{cmd}"')
        lines.append('decision = "deny"')
        lines.append(f"priority = {_GEMINI_DENY_PRIORITY}")
    return "\n".join(lines) + "\n"


def _unwrap_gemini_json(stdout: str) -> str:
    """Lift the worker's final report text out of Gemini's JSON envelope.

    ``gemini --output-format json`` wraps the reply in a FIXED
    ``{response, stats, error}`` envelope; the worker's final text (which then
    feeds :func:`parse_worker_report` upstream) is the ``response`` field. A
    malformed / non-JSON stdout (a crash before the envelope) is returned
    verbatim so the caller's report-parse still surfaces the worker's last
    words. There is no CLI decode schema for Gemini (``responseSchema`` is
    API/SDK-only), so this path leans entirely on the #304 floor — the path
    that motivates #304's repair loop.
    """
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout
    if isinstance(envelope, dict):
        response = envelope.get("response")
        if isinstance(response, str):
            return response
    return stdout


class GeminiCliInvoker:
    """Runs the worker as a fresh ``gemini -p`` child process.

    Transport (axis 1): the cacheable prefix is written to a tempfile referenced
    by ``GEMINI_SYSTEM_MD`` (a FULL replacement of Gemini's built-in system
    prompt), and the variable suffix is fed on stdin — both off argv (#169).
    Output comes from ``--output-format json``, whose fixed
    ``{response, stats, error}`` envelope is unwrapped to the worker's final
    text (:func:`_unwrap_gemini_json`), which then feeds ``parse_worker_report``
    upstream.

    Sandbox (axis 2): ``GEMINI_SANDBOX`` is deliberately left UNSET — it names a
    backend (``docker`` / ``podman`` / …), not a bool, so omitting it selects no
    sandbox. The worker must SSH/rsync out unattended.

    Tool fence (axis 3): a Policy Engine TOML (:func:`_gemini_policy_toml`)
    installed at the User/Admin tier (:func:`_gemini_policy_dir`) — NOT the
    workspace tier, which silently no-ops (#18186) — with the cluster-op deny at
    a higher ``priority`` than any allow (the Policy Engine is priority-ordered,
    not deny-beats-allow).

    Decode schema (axis 4): NONE at the CLI layer (``responseSchema`` is
    API/SDK-only), so the Gemini path leans entirely on the #304 floor
    (:func:`parse_worker_report`) — the path that motivates #304's repair loop.
    ``cache_stats`` is ``None`` (the CLI surfaces no cache-creation/cache-read
    split).
    """

    name = "gemini-cli"

    def __init__(self, *, executable: str = "gemini") -> None:
        self._executable = executable

    def invoke(
        self, prompt: RenderedPrompt, *, cwd: Path, report_cache_stats: bool = False
    ) -> InvocationResult:
        # report_cache_stats is accepted for Protocol conformance; Gemini's CLI
        # surfaces no cache-creation/cache-read split, so cache_stats stays None.
        policy_dir = _gemini_policy_dir()
        policy_dir.mkdir(parents=True, exist_ok=True)
        (policy_dir / "hpc-agent-worker.toml").write_text(_gemini_policy_toml(), encoding="utf-8")
        with tempfile.TemporaryDirectory(prefix="hpc-agent-gemini-") as work_dir:
            system_md = Path(work_dir) / "system.md"
            system_md.write_text(prompt.cacheable_prefix, encoding="utf-8")
            # GEMINI_SYSTEM_MD points at the full-replacement system prompt;
            # GEMINI_SANDBOX is intentionally NOT set (see class docstring).
            env = {**os.environ, "GEMINI_SYSTEM_MD": str(system_md)}
            env.pop("GEMINI_SANDBOX", None)
            proc = subprocess.run(
                [
                    self._executable,
                    "-p",
                    # Pin the CONCRETE model id, not an alias (aliases now
                    # resolve to a preview generation).
                    "--model",
                    _worker_model(_GEMINI_WORKER_MODEL_ENV, _GEMINI_WORKER_MODEL),
                    # The fixed {response, stats, error} envelope, unwrapped below.
                    "--output-format",
                    "json",
                ],
                # The variable suffix is the user prompt on stdin (the prefix is
                # the GEMINI_SYSTEM_MD system prompt); both stay off argv (#169).
                input=prompt.variable_suffix,
                cwd=str(cwd),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        return InvocationResult(
            exit_code=proc.returncode,
            output=_unwrap_gemini_json(proc.stdout),
            stderr=getattr(proc, "stderr", None) or "",
            cache_stats=None,
        )

    def missing_credential_remediation(self) -> str | None:
        if any(os.environ.get(var) for var in _GEMINI_CREDENTIAL_ENV_VARS):
            return None
        return _GEMINI_MISSING_CREDENTIAL_REMEDIATION


_INVOKERS: dict[str, Callable[..., WorkerInvoker]] = {
    "claude-cli": ClaudeCliInvoker,
    "claude-cli-oauth": ClaudeCliOAuthInvoker,
    "codex-cli": CodexCliInvoker,
    "gemini-cli": GeminiCliInvoker,
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

    Claude credentials always win, unchanged: ``ANTHROPIC_API_KEY`` /
    cloud-provider creds present → the proven ``--bare`` ``claude-cli`` path;
    otherwise a Claude Code OAuth credentials file on a supported OS →
    ``claude-cli-oauth``.

    Only when NO Claude credential is present does selection fall through to the
    other harnesses: a ``CODEX_API_KEY`` → ``codex-cli``, then a
    ``GEMINI_API_KEY`` / ``GOOGLE_API_KEY`` → ``gemini-cli``. The final fallback
    is unchanged: ``claude-cli`` (``DEFAULT_INVOKER``) so its pre-spawn
    credential guard fires with an actionable message rather than silently
    picking a path that cannot authenticate.
    """
    if any(os.environ.get(var) for var in _WORKER_CREDENTIAL_ENV_VARS):
        return "claude-cli"
    if _oauth_credentials_available():
        return "claude-cli-oauth"
    if os.environ.get(_CODEX_CREDENTIAL_ENV_VAR):
        return "codex-cli"
    if any(os.environ.get(var) for var in _GEMINI_CREDENTIAL_ENV_VARS):
        return "gemini-cli"
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
