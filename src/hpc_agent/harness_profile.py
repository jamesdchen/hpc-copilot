"""The declarative harness-activation profile — WHAT to wire, not HOW.

Activation (installing hpc-agent's capability *providers* into a harness's
config) was Claude-Code-only: :func:`hpc_agent.agent_assets.install_agent_assets`
hand-built ``settings.json`` / ``.claude.json`` inline. This module lifts the
*declarative* content of that install — the hook inventory, the fused Stop
dispatcher, and the MCP server invocation — into a frozen
:class:`HarnessProfile` whose fields carry **mechanism description only**, and a
:class:`ClaudeCodeProfile` renderer that maps that neutral description into
Claude Code's exact config layout (byte-identical to the pre-refactor install —
pinned by ``tests/cli/test_profile_golden.py``).

**Doctrine — the profile is MECHANISM, never AUTHORIZATION (activation plan
§5-R3 / premortem D6).** A ``HarnessProfile`` describes which providers to wire;
installing it grants ZERO trust. It carries no ``capabilities`` /
``provides`` / ``grants`` / ``trust`` / ``conformant`` field — a self-asserted
capability manifest is the named failure shape and is impossible by construction
(the field set is CLOSED and frozen, pinned by
``tests/contracts/test_harness_profile_boundary.py``). Capability presence is
proven only by BEHAVIOR (the conformance kit's ``declared == detected ==
behaved``) and read only from the DETECTED settings-seam
(:func:`hpc_agent.ops.harness_capabilities`) — never from "a profile was
installed." No code in the trust path (gates / verify / journal) imports or
reads this module; that too is a fired pin (the consumer-trace AST test).

**Neutral event semantics.** Hook descriptors name their turn-boundary event in
harness-neutral terms (:class:`HookEvent`: ``session-start`` / ``on-prompt`` /
``pre-tool`` / ``post-tool`` / ``turn-final``) and their tool-matcher intent
(:class:`ToolClass`: ``none`` / ``shell`` / ``question``). A foreign harness
maps those to its OWN event model; :class:`ClaudeCodeProfile` maps them to Claude
Code's ``Stop`` / ``PreToolUse`` / ``PostToolUse`` / ``UserPromptSubmit`` /
``SessionStart`` strings and its ``Bash`` / ``AskUserQuestion`` matchers.

**The needle-embed obligation on ANY renderer (activation plan §5-R2 /
premortem D8).** Each descriptor carries its hook module path as the ``needle``
— load-bearing DATA: it is written INTO the rendered command AND is the key the
capability probe (:func:`hpc_agent.ops.harness_capabilities`) and the re-find /
self-heal matcher (:func:`hpc_agent.agent_assets._find_hook_entry_index`) match
an installed entry on. **A conforming renderer — ours OR a foreign one — MUST
embed the descriptor's ``needle`` substring verbatim in the command it emits**,
whatever invocation shape it chooses (``python -m <needle>``, a wrapper script,
a container exec). A renderer that drops the needle installs hooks the
capability probe can never see: the harness self-reports the capability ABSENT
while its hooks are in fact wired, and our re-find/self-heal never converges.
This is a *documented contract* on the renderer, not an enforced seam — no
foreign-renderer seam exists yet (it lands with the Wave-C adapters); our own
:class:`ClaudeCodeProfile` satisfies it and the golden test pins that every
rendered command mentions its needle.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from enum import Enum
from typing import Any

__all__ = [
    "ClaudeCodeProfile",
    "HarnessProfile",
    "HookDescriptor",
    "HookEvent",
    "McpServerDescriptor",
    "StopMultiplexDescriptor",
    "ToolClass",
]


class HookEvent(Enum):
    """A turn-boundary event named in harness-neutral terms.

    A foreign harness maps each to whatever its own event model calls the same
    moment; :class:`ClaudeCodeProfile` maps them to Claude Code's event strings.
    """

    SESSION_START = "session-start"  # a session begins
    ON_PROMPT = "on-prompt"  # a human prompt is submitted
    PRE_TOOL = "pre-tool"  # before a tool call
    POST_TOOL = "post-tool"  # after a tool call
    TURN_FINAL = "turn-final"  # the agent is about to end its turn


class ToolClass(Enum):
    """The tool a hook filters on, named neutrally (the matcher INTENT).

    ``NONE`` — the event has no tool to match (session/prompt/turn events).
    ``SHELL`` — a shell/command tool (Claude Code: ``Bash``).
    ``QUESTION`` — a structured user-question tool (Claude Code: ``AskUserQuestion``).
    ``OWN_TOOLS`` — the harness's projection of hpc-agent's OWN verb surface,
    whatever that harness calls it (Claude Code: the MCP tools
    ``mcp__hpc-agent__*``). The intent is "our verbs, invoked as tools" — a
    foreign harness with a different projection maps it to that projection's
    matcher.
    """

    NONE = "none"
    SHELL = "shell"
    QUESTION = "question"
    OWN_TOOLS = "own-tools"


@dataclass(frozen=True)
class HookDescriptor:
    """One capability-provider hook to wire, described neutrally.

    * ``needle`` — the hook module path; load-bearing DATA embedded in the
      rendered command and matched on by the capability probe / re-find (see the
      module docstring's needle-embed obligation).
    * ``event`` — the neutral turn-boundary event.
    * ``tool_class`` — the neutral tool-matcher intent.
    * ``prefilter`` — trigger substrings; when non-empty the renderer may gate
      the interpreter start on the payload containing one of them (an
      optimization, never a semantic — a false positive costs one no-op start).
    """

    needle: str
    event: HookEvent
    tool_class: ToolClass
    prefilter: tuple[str, ...]


@dataclass(frozen=True)
class StopMultiplexDescriptor:
    """The fused turn-final dispatcher: one entry running several guards.

    ``needle`` is the multiplex module; ``guards`` the guard module paths it
    dispatches (each of which is ALSO a needle the capability probe keys on, so
    the rendered command must mention all of them).
    """

    needle: str
    guards: tuple[str, ...]


@dataclass(frozen=True)
class McpServerDescriptor:
    """The MCP server invocation as neutral data (no resolved interpreter path).

    ``name`` the server id; ``module`` the ``-m`` target; ``args`` the argv after
    it. The interpreter is NOT part of the descriptor — a renderer resolves it at
    render time (Claude Code: ``sys.executable``), keeping the descriptor neutral
    and machine-independent.
    """

    name: str
    module: str
    args: tuple[str, ...]


@dataclass(frozen=True)
class HarnessProfile:
    """The declarative activation profile: WHAT providers to wire.

    CLOSED, frozen field set (mechanism description only — see the module
    docstring's doctrine and the boundary test). NO capability-assertion field
    exists by construction.

    * ``hook_descriptors`` — the non-fused hooks, in install order (append order
      within an event is load-bearing).
    * ``stop_hook`` — the fused turn-final dispatcher.
    * ``mcp_server`` — the MCP server invocation as data.
    * ``asset_package`` — the importlib package holding the core
      ``commands/`` + ``skills/`` + ``agents/`` asset trees to distribute.
    """

    hook_descriptors: tuple[HookDescriptor, ...]
    stop_hook: StopMultiplexDescriptor
    mcp_server: McpServerDescriptor
    asset_package: str


class ClaudeCodeProfile:
    """Renders a :class:`HarnessProfile` into Claude Code's config layout.

    The FIRST profile renderer (activation plan §2b). Pure and stateless: every
    method is a function of the profile + the injected interpreter path
    (``executable``), so the render is a pure function over hermetic inputs — the
    basis of the golden byte-identity pin (premortem D5). Core ships only this
    renderer (RD-1); a foreign harness ships its own, reading the same descriptors
    and honoring the needle-embed obligation.
    """

    _EVENT_STRINGS: dict[HookEvent, str] = {
        HookEvent.SESSION_START: "SessionStart",
        HookEvent.ON_PROMPT: "UserPromptSubmit",
        HookEvent.PRE_TOOL: "PreToolUse",
        HookEvent.POST_TOOL: "PostToolUse",
        HookEvent.TURN_FINAL: "Stop",
    }
    _MATCHER_STRINGS: dict[ToolClass, str | None] = {
        ToolClass.NONE: None,
        ToolClass.SHELL: "Bash",
        ToolClass.QUESTION: "AskUserQuestion",
        # Claude Code names a projected MCP server's tools
        # ``mcp__<server>__<tool>`` and accepts a regex matcher; this one covers
        # every verb the ``hpc-agent`` server projects. The server id matches
        # :data:`hpc_agent.agent_assets.CLAUDE_CODE_PROFILE`'s
        # ``McpServerDescriptor.name`` — the same renderer writes both, and the
        # golden pins them together.
        ToolClass.OWN_TOOLS: "mcp__hpc-agent__.*",
    }

    @classmethod
    def event_string(cls, event: HookEvent) -> str:
        """The Claude Code ``settings.json`` event name for a neutral event."""
        return cls._EVENT_STRINGS[event]

    @classmethod
    def matcher_string(cls, tool_class: ToolClass) -> str | None:
        """The Claude Code ``matcher`` string for a neutral tool intent (``None`` = none)."""
        return cls._MATCHER_STRINGS[tool_class]

    @staticmethod
    def hook_python(executable: str) -> str:
        """Bash-safe interpreter path for a hook command (Windows backslash/space fix).

        Claude Code runs hooks via ``bash -c '<command>'``. On Windows a raw
        ``sys.executable`` carries backslashes bash treats as escapes (``C:\\U`` →
        ``C:U`` → "command not found") and may contain spaces; forward slashes are
        accepted for executable invocation on Windows and pass bash unchanged, and
        ``shlex.quote`` wraps a spaced path.
        """
        return shlex.quote(executable.replace("\\", "/"))

    @classmethod
    def hook_command(cls, descriptor: HookDescriptor, executable: str) -> str:
        """The ``bash -c`` command string for one hook descriptor.

        Bare ``<py> -m <needle>`` when the descriptor has no pre-filter; otherwise
        a bash ``case`` gate that only pipes the payload into Python when it
        contains one of the ``prefilter`` substrings (keeping the common path at
        bash-builtin cost). The ``needle`` is embedded verbatim either way (the
        needle-embed obligation).
        """
        py = cls.hook_python(executable)
        if not descriptor.prefilter:
            return f"{py} -m {descriptor.needle}"
        pattern = "|".join(f"*{verb}*" for verb in descriptor.prefilter)
        return (
            'input=$(cat); case "$input" in '
            f"{pattern}) "
            f"printf '%s' \"$input\" | {py} "
            f"-m {descriptor.needle};; esac"
        )

    @classmethod
    def stop_command(cls, stop: StopMultiplexDescriptor, executable: str) -> str:
        """The fused turn-final command: ``<py> -m <multiplex> <guard> <guard> …``.

        Names each guard module explicitly as an argument, so the fused command
        mentions every guard needle (the capability probe / re-find resolve
        against it) as well as the multiplex needle.
        """
        py = cls.hook_python(executable)
        guards = " ".join(stop.guards)
        return f"{py} -m {stop.needle} {guards}"

    @staticmethod
    def hook_entry(command: str, *, matcher: str | None) -> dict[str, Any]:
        """A single ``settings.json`` hook entry running *command*.

        ``{"matcher": …, "hooks": […]}`` when *matcher* is set (tool-matched
        events), the matcher-less ``{"hooks": […]}`` otherwise. Key order —
        matcher before hooks — is preserved so the written JSON is byte-stable.
        """
        entry: dict[str, Any] = {}
        if matcher is not None:
            entry["matcher"] = matcher
        entry["hooks"] = [{"type": "command", "command": command}]
        return entry

    @classmethod
    def render_hook_entry(cls, descriptor: HookDescriptor, executable: str) -> dict[str, Any]:
        """The full ``settings.json`` entry for one hook descriptor."""
        return cls.hook_entry(
            cls.hook_command(descriptor, executable),
            matcher=cls.matcher_string(descriptor.tool_class),
        )

    @classmethod
    def render_stop_entry(cls, stop: StopMultiplexDescriptor, executable: str) -> dict[str, Any]:
        """The fused turn-final ``settings.json`` entry (matcher-less)."""
        return cls.hook_entry(cls.stop_command(stop, executable), matcher=None)

    @classmethod
    def render_mcp_entry(cls, mcp: McpServerDescriptor, executable: str) -> dict[str, Any]:
        """The ``.claude.json`` ``mcpServers`` entry for the MCP descriptor.

        Key order — type, command, args — is preserved for byte-stability.
        """
        return {
            "type": "stdio",
            "command": executable,
            "args": ["-m", mcp.module, *mcp.args],
        }
