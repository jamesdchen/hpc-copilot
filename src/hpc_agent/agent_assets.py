"""Install hpc-agent's bundled slash commands and skills into ``~/.claude/``.

The CLI surface is ``hpc-agent install-commands`` and lives in
:mod:`hpc_agent.cli.setup`; this module provides the copy logic so a pip-only
install (no repo checkout) can still wire the agent assets into Claude
Code's user-global config directory.

The core asset trees ship as package data inside the ``slash_commands``
package — ``slash_commands/commands/*.md``,
``slash_commands/skills/<name>/SKILL.md``, and
``slash_commands/agents/<name>.md`` (named subagent definitions, e.g.
the haiku-pinned ``hpc-worker`` that inline mode dispatches to).
Optional plugins may ship their own ``commands/`` + ``skills/`` +
``agents/`` trees via the ``slash_command_assets`` hook on the
``hpc_agent.plugins`` seam; those are installed *after* the core
assets, so a plugin's copy of an asset overrides the core one of the
same name (last writer wins by path).
"""

from __future__ import annotations

import json
import shlex
import shutil
import sys
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any

__all__ = ["DEFAULT_CLAUDE_DIR", "install_agent_assets"]


def _hook_python() -> str:
    """Bash-safe path to the current Python interpreter for hook commands.

    Claude Code runs hooks via ``bash -c '<command>'``. Two Windows pitfalls
    the raw ``sys.executable`` walks into:

    * **Backslashes.** ``sys.executable`` is a native backslash path on Windows
      (e.g. ``C:\\Users\\james\\.venv\\Scripts\\python.exe``). Bash treats
      ``\\U``, ``\\j``, ``\\d`` etc. as escape sequences and collapses the
      backslash, turning the path into ``C:Usersjames.venvScriptspython.exe``
      → "command not found". Forward slashes are universally accepted by
      Windows for executable invocation and pass through bash unchanged.
    * **Spaces.** Some interpreter paths contain spaces (e.g.
      ``C:/Program Files/Python311/python.exe`` or a repo dir with a space).
      Without quoting bash splits on the space and tries to run a non-existent
      first token. ``shlex.quote`` wraps the path in single quotes when needed.
    """
    return shlex.quote(sys.executable.replace("\\", "/"))


def _build_hook_command() -> str:
    """The autofetch ``PostToolUse`` hook command, with a bash-level pre-filter.

    ``matcher: "Bash"`` fires this hook after **every** Bash call, and a
    Python interpreter start costs ~300-500ms on Windows (#288). The ``case``
    pre-filter keeps the non-emit common path at bash-builtin cost: only a
    payload mentioning ``emit-skill-return`` reaches Python. (The substring
    scan over the whole payload can false-positive on e.g. an unrelated
    command echoing the verb name — that costs one no-op interpreter start,
    nothing more.)
    """
    return (
        'input=$(cat); case "$input" in *emit-skill-return*) '
        f"printf '%s' \"$input\" | {_hook_python()} "
        "-m hpc_agent._kernel.hooks.skill_return_autofetch;; esac"
    )


def _build_stop_hook_command() -> str:
    """The stop-guard ``Stop`` hook command — no pre-filter.

    Stop fires once per turn (not per tool call), so the interpreter start is
    paid rarely; the guard itself needs the filesystem probe either way.
    """
    return f"{_hook_python()} -m hpc_agent._kernel.hooks.skill_return_stop_guard"


def _build_rendezvous_autofetch_command() -> str:
    """The decision-rendezvous ``PostToolUse`` hook command, with a pre-filter.

    Mirrors :func:`_build_hook_command`: ``matcher: "Bash"`` fires after every
    Bash call, so a ``case`` pre-filter keeps the non-``block-drive`` common
    path at bash-builtin cost — only a payload mentioning ``block-drive``
    reaches Python (which then reads the freshly-parked brief back).
    """
    return (
        'input=$(cat); case "$input" in *block-drive*) '
        f"printf '%s' \"$input\" | {_hook_python()} "
        "-m hpc_agent._kernel.hooks.decision_rendezvous_autofetch;; esac"
    )


def _build_rendezvous_stop_hook_command() -> str:
    """The decision-rendezvous ``Stop`` guard command — no pre-filter.

    Sibling of :func:`_build_stop_hook_command`: Stop fires once per turn, so
    the interpreter start is paid rarely and the guard needs the journal probe
    either way.
    """
    return f"{_hook_python()} -m hpc_agent._kernel.hooks.decision_rendezvous_stop_guard"


# The ``PostToolUse`` hook that auto-fetches a sub-skill's return envelope the
# moment the sub-skill's ``emit-skill-return`` Bash call commits it (see
# :mod:`hpc_agent._kernel.hooks.skill_return_autofetch` for why the trigger is
# the emit Bash call and not the Skill tool — the Skill tool returns *before*
# the sub-skill body runs, so a Skill-matched hook can never see a fresh
# envelope). install-commands merges this entry into
# ``~/.claude/settings.json``'s ``hooks.PostToolUse`` array, additively,
# idempotently, and self-healing on a stale prior install (including the
# pre-0.10.58 ``matcher: "Skill"`` shape).
_HOOK_COMMAND = _build_hook_command()
_AUTOFETCH_NEEDLE = "hpc_agent._kernel.hooks.skill_return_autofetch"
_SKILL_RETURN_HOOK_ENTRY: dict[str, Any] = {
    "matcher": "Bash",
    "hooks": [
        {
            "type": "command",
            "command": _HOOK_COMMAND,
        }
    ],
}

# The ``Stop`` hook that blocks ending the turn while a committed sub-skill
# return envelope sits unfetched (see
# :mod:`hpc_agent._kernel.hooks.skill_return_stop_guard`). Deterministic
# backstop for the advisory hand-back prose at sub-skill composition
# boundaries. Stop entries take no matcher (there is no tool to match).
_STOP_HOOK_COMMAND = _build_stop_hook_command()
_STOP_GUARD_NEEDLE = "hpc_agent._kernel.hooks.skill_return_stop_guard"
_SKILL_RETURN_STOP_ENTRY: dict[str, Any] = {
    "hooks": [
        {
            "type": "command",
            "command": _STOP_HOOK_COMMAND,
        }
    ],
}

# The ``block-drive`` decision-rendezvous pair (see
# :mod:`hpc_agent._kernel.hooks.decision_rendezvous_autofetch` /
# ``decision_rendezvous_stop_guard``), generalizing the skill-return pair to the
# §5 y/nudge boundary. The PostToolUse autofetch injects the brief a
# ``block-drive`` tick just parked; the Stop guard blocks ending the turn once a
# human ``y`` is committed but the driver has not advanced. Both merge additively
# + idempotently, matched on the module-path needle.
_RENDEZVOUS_AUTOFETCH_COMMAND = _build_rendezvous_autofetch_command()
_RENDEZVOUS_AUTOFETCH_NEEDLE = "hpc_agent._kernel.hooks.decision_rendezvous_autofetch"
_RENDEZVOUS_AUTOFETCH_ENTRY: dict[str, Any] = {
    "matcher": "Bash",
    "hooks": [
        {
            "type": "command",
            "command": _RENDEZVOUS_AUTOFETCH_COMMAND,
        }
    ],
}

_RENDEZVOUS_STOP_COMMAND = _build_rendezvous_stop_hook_command()
_RENDEZVOUS_STOP_NEEDLE = "hpc_agent._kernel.hooks.decision_rendezvous_stop_guard"
_RENDEZVOUS_STOP_ENTRY: dict[str, Any] = {
    "hooks": [
        {
            "type": "command",
            "command": _RENDEZVOUS_STOP_COMMAND,
        }
    ],
}

# The registry-projected MCP server (``hpc-agent mcp-serve``) — the preferred,
# shell-free invocation surface for blocks (design §3, "The tool surface subsumes
# the shell"). Registered venv-pinned via the current interpreter so it does not
# depend on ``hpc-agent`` being on PATH. ``--allow-mutations`` exposes the
# submit/aggregate verbs (cancel/raw-submit are never registry primitives, so they
# stay unreachable either way); ``--catalog curated`` advertises exactly the
# human-amplification block verbs (those returning a next_block) plus the
# recovery/opt-in verbs (doctor, kill, submit-speculate), keeping the rest of the
# catalog out of the model's context.
_MCP_SERVER_NAME = "hpc-agent"
_MCP_SERVER_ENTRY: dict[str, Any] = {
    "type": "stdio",
    "command": sys.executable,
    "args": ["-m", "hpc_agent", "mcp-serve", "--allow-mutations", "--catalog", "curated"],
}


def _mcp_config_path(claude_dir: Path) -> Path:
    """Where Claude Code reads user-global MCP servers from: ``.claude.json``.

    Claude Code discovers user-global MCP servers in ``~/.claude.json`` (the
    top-level ``mcpServers`` object), a SIBLING of the ``~/.claude`` config dir —
    not inside it. So the path is derived from *claude_dir*'s parent, which makes
    it hermetic under a test-supplied ``claude_dir`` (``tmp/.claude`` →
    ``tmp/.claude.json``) while resolving to ``~/.claude.json`` for the default
    install. (A user who relocates the whole config via ``CLAUDE_CONFIG_DIR`` and
    passes that as ``claude_dir`` gets ``.claude.json`` alongside it, which is the
    same sibling relationship.)
    """
    return claude_dir.parent / ".claude.json"


def _register_mcp_server(claude_dir: Path, *, dry_run: bool) -> dict[str, Any]:
    """Additively, idempotently register the ``hpc-agent`` MCP server in
    ``.claude.json``'s ``mcpServers`` object.

    Same contract as :func:`_merge_hook_entry`, targeting ``mcpServers`` in
    ``.claude.json`` rather than ``hooks`` in ``settings.json``: creates an empty
    ``{}`` model when the file is absent/unreadable, refuses to clobber a present
    file that is not a JSON object (``skipped-unparseable``), and only ever adds
    or in-place heals our one ``hpc-agent`` entry — every other server and key is
    preserved verbatim. Idempotent: a byte-equal entry is ``already-present``; a
    stale entry (e.g. a moved venv changing the interpreter path) is ``updated``.

    Returns ``{config_path, action, wrote}`` where ``action`` is ``"added"`` /
    ``"updated"`` / ``"already-present"`` / ``"skipped-unparseable"`` /
    ``"dry-run-would-add"`` / ``"dry-run-would-update"``.
    """
    config_path = _mcp_config_path(claude_dir)

    config: dict[str, Any]
    if config_path.exists():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return {
                "config_path": str(config_path),
                "action": "skipped-unparseable",
                "wrote": False,
            }
        if not isinstance(loaded, dict):
            return {
                "config_path": str(config_path),
                "action": "skipped-unparseable",
                "wrote": False,
            }
        config = loaded
    else:
        config = {}

    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}

    existing = servers.get(_MCP_SERVER_NAME)
    if existing == _MCP_SERVER_ENTRY:
        return {"config_path": str(config_path), "action": "already-present", "wrote": False}

    if dry_run:
        action = "dry-run-would-update" if existing is not None else "dry-run-would-add"
        return {"config_path": str(config_path), "action": action, "wrote": False}

    servers = dict(servers)
    action = "updated" if existing is not None else "added"
    servers[_MCP_SERVER_NAME] = _MCP_SERVER_ENTRY
    config["mcpServers"] = servers

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return {"config_path": str(config_path), "action": action, "wrote": True}


def DEFAULT_CLAUDE_DIR() -> Path:
    """Return ``~/.claude`` (does not create the directory)."""
    return Path.home() / ".claude"


def _find_hook_entry_index(entries: list[Any], needle: str) -> int | None:
    """Return the index of the existing entry matching *needle*, or ``None``.

    Match key: any hook entry whose ``hooks`` list contains a ``command`` hook
    whose command mentions *needle* (a hook module path). We match on the
    module path (not the full command string) so a re-run from a different
    ``sys.executable`` — moved venv, **an upgrade that fixes the command
    encoding**, or one that changes the matcher/pre-filter shape — still finds
    the existing entry instead of appending a duplicate.
    """
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        hooks = entry.get("hooks")
        if not isinstance(hooks, list):
            continue
        for hook in hooks:
            if (
                isinstance(hook, dict)
                and hook.get("type") == "command"
                and isinstance(hook.get("command"), str)
                and needle in hook["command"]
            ):
                return i
    return None


def _merge_hook_entry(
    claude_dir: Path, *, event: str, entry: dict[str, Any], needle: str, dry_run: bool
) -> dict[str, Any]:
    """Additively, idempotently wire one hook *entry* into ``settings.json``.

    Reads ``<claude_dir>/settings.json`` (creating an empty ``{}`` model when it
    is absent or unreadable), appends *entry* to ``hooks.<event>`` unless an
    equivalent entry (matched by *needle*, the hook's module path) is already
    present, and writes the merged settings back (pretty-printed, trailing
    newline). Every other key and every other entry under *event* is preserved
    verbatim — the merge only ever *adds* (or in-place heals) our one entry.

    Returns a small report ``{settings_path, action, wrote}`` where ``action``
    is one of ``"added"`` (appended), ``"updated"`` (a stale entry from an
    earlier install — e.g. a moved venv, the pre-0.10.10 backslash-encoded
    Windows path that bash mis-interpreted as escapes, or the pre-0.10.58
    ``matcher: "Skill"`` autofetch shape — replaced in place),
    ``"already-present"`` (byte-equal to the canonical entry; idempotent
    no-op), ``"skipped-unparseable"`` (existing settings.json is not a JSON
    object — we refuse to clobber it), ``"dry-run-would-add"``, or
    ``"dry-run-would-update"``.

    Safety: if ``settings.json`` exists but does not parse as a JSON **object**,
    we do **not** overwrite it — the install reports ``skipped-unparseable`` so
    a human can resolve it rather than risking the loss of hand-written config.
    """
    settings_path = claude_dir / "settings.json"

    settings: dict[str, Any]
    if settings_path.exists():
        try:
            loaded = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            # Present but unreadable / not JSON. A settings.json is precious
            # user config; refuse to clobber it rather than guess.
            return {
                "settings_path": str(settings_path),
                "action": "skipped-unparseable",
                "wrote": False,
            }
        if not isinstance(loaded, dict):
            return {
                "settings_path": str(settings_path),
                "action": "skipped-unparseable",
                "wrote": False,
            }
        settings = loaded
    else:
        settings = {}

    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        # Absent or wrong-typed ``hooks`` → start a fresh mapping. (A non-dict
        # ``hooks`` would itself be malformed Claude config; replacing it is the
        # only way to wire our entry, and we only do so when adding.)
        hooks = {}
    event_entries = hooks.get(event)
    if not isinstance(event_entries, list):
        event_entries = []

    existing_idx = _find_hook_entry_index(event_entries, needle)
    if existing_idx is not None and event_entries[existing_idx] == entry:
        return {
            "settings_path": str(settings_path),
            "action": "already-present",
            "wrote": False,
        }

    if dry_run:
        return {
            "settings_path": str(settings_path),
            "action": "dry-run-would-update" if existing_idx is not None else "dry-run-would-add",
            "wrote": False,
        }

    event_entries = list(event_entries)
    if existing_idx is not None:
        event_entries[existing_idx] = entry
        action = "updated"
    else:
        event_entries.append(entry)
        action = "added"
    hooks[event] = event_entries
    settings["hooks"] = hooks

    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(settings, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    return {
        "settings_path": str(settings_path),
        "action": action,
        "wrote": True,
    }


def _skill_allow_rule(skill_name: str) -> str:
    """Return the canonical ``permissions.allow`` entry for a Skill grant.

    Mirrors the ``Bash(<prefix>:*)`` parameterised matcher format Claude Code
    uses for the existing precedent in ``ops/memory/interview.py``'s
    ``_maybe_write_claude_permissions`` (which grants ``Bash(hpc-agent:*)``).
    For Skill, the natural matcher is the skill name, so the entry is
    ``Skill(<name>)`` — narrowest grant per bundled skill rather than a
    blanket ``"Skill"``.
    """
    return f"Skill({skill_name})"


def _merge_skill_permissions(
    claude_dir: Path, skill_names: list[str], *, dry_run: bool
) -> dict[str, Any]:
    """Idempotently add ``Skill(<name>)`` allow rules for every installed skill.

    Without these grants, Claude Code's auto-mode classifier silently denies
    the first ``Skill(<name>)`` call from ``/submit-hpc`` /
    ``/aggregate-hpc`` / ``/monitor-hpc`` / ``/campaign-hpc`` (empirical
    2026-06-06 demo: ``Skill(hpc-submit)`` blocked with ``Denied by auto
    mode classifier`` despite ``skipAutoPermissionPrompt: true`` — the flag
    only suppresses the explicit prompt, the classifier still gates).

    Sibling of :func:`_merge_hook_entry`: same additive + idempotent
    + skip-unparseable + dry-run semantics, but targets
    ``permissions.allow`` rather than ``hooks.PostToolUse``. User-global
    scope here (the bundled skills are user-global; the orchestrator can
    invoke ``/submit-hpc`` from any working directory) — distinct from
    ``ops/memory/interview.py``'s project-scoped Bash grant (#190).

    Returns ``{settings_path, action, added, wrote}`` where ``action`` is:

    * ``"added"`` — at least one new ``Skill(<name>)`` rule appended
    * ``"already-present"`` — every rule already in ``permissions.allow``
    * ``"skipped-unparseable"`` — existing settings.json is not a JSON object
    * ``"dry-run-would-add"`` — would have added rules but ``dry_run=True``

    ``added`` lists the rule strings actually appended (empty on
    ``"already-present"``); on dry-run, lists the strings that *would*
    have been added.
    """
    settings_path = claude_dir / "settings.json"

    settings: dict[str, Any]
    if settings_path.exists():
        try:
            loaded = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return {
                "settings_path": str(settings_path),
                "action": "skipped-unparseable",
                "added": [],
                "wrote": False,
            }
        if not isinstance(loaded, dict):
            return {
                "settings_path": str(settings_path),
                "action": "skipped-unparseable",
                "added": [],
                "wrote": False,
            }
        settings = loaded
    else:
        settings = {}

    permissions = settings.get("permissions")
    if not isinstance(permissions, dict):
        permissions = {}
    allow = permissions.get("allow")
    if not isinstance(allow, list):
        allow = []

    canonical_rules = [_skill_allow_rule(name) for name in skill_names]
    missing = [rule for rule in canonical_rules if rule not in allow]

    if not missing:
        return {
            "settings_path": str(settings_path),
            "action": "already-present",
            "added": [],
            "wrote": False,
        }

    if dry_run:
        return {
            "settings_path": str(settings_path),
            "action": "dry-run-would-add",
            "added": missing,
            "wrote": False,
        }

    allow = list(allow) + missing
    permissions["allow"] = allow
    settings["permissions"] = permissions

    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(settings, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    return {
        "settings_path": str(settings_path),
        "action": "added",
        "added": missing,
        "wrote": True,
    }


def _resolve_dir_collision(target: Path, kind_phrase: str, *, dry_run: bool) -> str | None:
    """Resolve a pre-existing non-directory at *target* before mkdir.

    The three install targets (``commands``/``skills``/``agents``) need to
    become directories. A pre-existing path collides; resolution depends on
    what's actually sitting there:

    * Missing or already a directory → nothing to do, returns ``None``.
    * Regular file with **zero bytes** → silently unlinked (or, in dry-run
      mode, just reported). A 0-byte file can't carry meaningful user
      content, and it's the empirically observed shape of stale scaffolding
      artifacts on Windows (touch-then-crash, abandoned old-version
      installs, etc.). Returns the cleared path as a string.
    * Any other non-directory → raises :class:`FileExistsError` with a
      clear remediation message. This is the historical guard preserved
      for the only case where the user might lose real content.

    *kind_phrase* is the inline phrase used in the error message
    (e.g. ``"slash commands"``).
    """
    if not target.exists() or target.is_dir():
        return None
    try:
        is_zero_byte = target.is_file() and target.stat().st_size == 0
    except OSError:
        is_zero_byte = False
    if is_zero_byte:
        if not dry_run:
            target.unlink()
        return str(target)
    raise FileExistsError(
        f"{target} exists but is not a directory — "
        f"hpc-agent setup needs to install {kind_phrase} here. "
        "Move or remove the conflicting file, then re-run."
    )


def _install_tree(
    root: Any, target: Path, *, dry_run: bool
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Copy one ``commands/`` + ``skills/`` + ``agents/`` asset tree rooted at *root*.

    *root* is any :mod:`importlib.resources` traversable. Returns
    ``(commands, skills, agents, cleared)`` — the command stems,
    skill-directory names, agent-definition stems found, and any
    pre-existing 0-byte collision paths that were auto-cleared so the
    install could proceed (see :func:`_resolve_dir_collision`). A missing
    ``commands/``, ``skills/`` or ``agents/`` subtree is skipped, so a
    plugin may contribute any subset of the three.
    """
    commands: list[str] = []
    skills: list[str] = []
    agents: list[str] = []
    cleared: list[str] = []

    commands_src = root / "commands"
    if commands_src.is_dir():
        commands_dst = target / "commands"
        cleared_path = _resolve_dir_collision(commands_dst, "slash commands", dry_run=dry_run)
        if cleared_path is not None:
            cleared.append(cleared_path)
        for entry in commands_src.iterdir():
            if not entry.name.endswith(".md"):
                continue
            commands.append(entry.name[:-3])
            if not dry_run:
                commands_dst.mkdir(parents=True, exist_ok=True)
                with as_file(entry) as real:
                    shutil.copy2(real, commands_dst / entry.name)

    skills_src = root / "skills"
    if skills_src.is_dir():
        skills_dst = target / "skills"
        cleared_path = _resolve_dir_collision(skills_dst, "skills", dry_run=dry_run)
        if cleared_path is not None:
            cleared.append(cleared_path)
        for skill in skills_src.iterdir():
            if not skill.is_dir():
                continue
            skills.append(skill.name)
            if not dry_run:
                skill_dst = skills_dst / skill.name
                skill_dst.mkdir(parents=True, exist_ok=True)
                for entry in skill.iterdir():
                    if entry.is_dir():
                        continue
                    with as_file(entry) as real:
                        shutil.copy2(real, skill_dst / entry.name)

    # Named subagent definitions — a flat ``agents/*.md`` tree (same shape
    # as ``commands/``). Claude Code discovers these under
    # ``~/.claude/agents/``; the haiku-pinned ``hpc-worker`` is what inline
    # mode dispatches to so the model pin rides with the definition (the
    # harness enforces it), not the caller's cooperation.
    agents_src = root / "agents"
    if agents_src.is_dir():
        agents_dst = target / "agents"
        cleared_path = _resolve_dir_collision(agents_dst, "agent definitions", dry_run=dry_run)
        if cleared_path is not None:
            cleared.append(cleared_path)
        for entry in agents_src.iterdir():
            if not entry.name.endswith(".md"):
                continue
            agents.append(entry.name[:-3])
            if not dry_run:
                agents_dst.mkdir(parents=True, exist_ok=True)
                with as_file(entry) as real:
                    shutil.copy2(real, agents_dst / entry.name)

    return commands, skills, agents, cleared


def install_agent_assets(
    *, claude_dir: Path | None = None, dry_run: bool = False
) -> dict[str, Any]:
    """Copy bundled slash commands, skills, and agent definitions into *claude_dir*.

    Slash commands land in ``<claude_dir>/commands/``, skills in
    ``<claude_dir>/skills/<name>/``, and named subagent definitions in
    ``<claude_dir>/agents/<name>.md``, overwriting any existing files.
    The core ``slash_commands`` assets are installed first; then any
    plugin exposing a ``slash_command_assets`` tree through the
    ``hpc_agent.plugins`` seam is installed over them, so an installed
    plugin's copy of an asset replaces the core one. With
    ``dry_run=True`` nothing is written — the returned dict still
    reports what would have been copied.

    Result shape::

        {
            "claude_dir": "<resolved path>",
            "commands_installed": ["aggregate-hpc", ...],
            "skills_installed": ["hpc-submit", ...],
            "agents_installed": ["hpc-worker", ...],
            "cleared_collisions": ["/.../.claude/agents", ...],
            "settings_hook": {"settings_path": "...", "action": "added", "wrote": <bool>},
            "settings_stop_hook": {"settings_path": "...", "action": "added", "wrote": <bool>},
            "settings_rendezvous_hook": {"settings_path": "...", "action": "added",
                                         "wrote": <bool>},
            "settings_rendezvous_stop_hook": {"settings_path": "...", "action": "added",
                                              "wrote": <bool>},
            "settings_permissions": {"settings_path": "...", "action": "added",
                                     "added": ["Skill(hpc-submit)", ...], "wrote": <bool>},
            "mcp_server": {"config_path": "...", "action": "added", "wrote": <bool>},
            "wrote": <bool>,
        }

    ``mcp_server`` reports the additive, idempotent registration of the
    ``hpc-agent`` MCP server into ``.claude.json``'s ``mcpServers`` — see
    :func:`_register_mcp_server`.

    ``cleared_collisions`` lists any pre-existing 0-byte files at
    ``<claude>/commands``/``skills``/``agents`` that were silently
    removed before mkdir — see :func:`_resolve_dir_collision`. Non-empty
    collisions still raise :class:`FileExistsError`.

    ``settings_hook`` reports the additive, idempotent merge of the
    skill-return autofetch ``PostToolUse`` hook into
    ``<claude>/settings.json``, and ``settings_stop_hook`` the same for the
    skill-return ``Stop`` guard — see :func:`_merge_hook_entry`. Each
    ``action`` is ``"added"`` / ``"already-present"`` / ``"updated"`` /
    ``"skipped-unparseable"`` / ``"dry-run-would-add"`` / ``"dry-run-would-update"``.

    ``settings_permissions`` reports the additive, idempotent merge of
    ``Skill(<name>)`` allow rules for every installed skill into
    ``<claude>/settings.json``'s ``permissions.allow`` — see
    :func:`_merge_skill_permissions`. Its ``action`` is ``"added"`` /
    ``"already-present"`` / ``"skipped-unparseable"`` /
    ``"dry-run-would-add"``, and ``added`` lists the rule strings
    actually appended (or that *would* have been on dry-run).
    """
    target = (claude_dir or DEFAULT_CLAUDE_DIR()).expanduser()

    commands: set[str] = set()
    skills: set[str] = set()
    agents: set[str] = set()
    cleared: list[str] = []

    core_commands, core_skills, core_agents, core_cleared = _install_tree(
        files("slash_commands"), target, dry_run=dry_run
    )
    commands.update(core_commands)
    skills.update(core_skills)
    agents.update(core_agents)
    cleared.extend(core_cleared)

    # Optional plugins overlay their own assets last — a plugin's
    # skills/<name>/ (or agents/<name>.md) overrides the core copy of the
    # same name.
    from hpc_agent._kernel.registry.plugins import plugin_slash_command_roots

    for root in plugin_slash_command_roots():
        plugin_commands, plugin_skills, plugin_agents, plugin_cleared = _install_tree(
            root, target, dry_run=dry_run
        )
        commands.update(plugin_commands)
        skills.update(plugin_skills)
        agents.update(plugin_agents)
        cleared.extend(plugin_cleared)

    # Wire the skill-return hooks into settings.json — additive + idempotent,
    # never clobbering existing hooks/keys. Two entries: the PostToolUse
    # autofetch (injects the envelope the moment emit-skill-return commits it)
    # and the Stop guard (blocks ending the turn while an envelope sits
    # unfetched — the deterministic backstop for the advisory hand-back prose).
    settings_hook = _merge_hook_entry(
        target,
        event="PostToolUse",
        entry=_SKILL_RETURN_HOOK_ENTRY,
        needle=_AUTOFETCH_NEEDLE,
        dry_run=dry_run,
    )
    settings_stop_hook = _merge_hook_entry(
        target,
        event="Stop",
        entry=_SKILL_RETURN_STOP_ENTRY,
        needle=_STOP_GUARD_NEEDLE,
        dry_run=dry_run,
    )

    # Wire the block-drive decision-rendezvous pair (§5): the PostToolUse
    # autofetch injects the brief a block-drive tick just parked; the Stop guard
    # forces the driver to advance once a human y is committed but unconsumed.
    # Same additive + idempotent merge, matched on their own module-path needles.
    settings_rendezvous_hook = _merge_hook_entry(
        target,
        event="PostToolUse",
        entry=_RENDEZVOUS_AUTOFETCH_ENTRY,
        needle=_RENDEZVOUS_AUTOFETCH_NEEDLE,
        dry_run=dry_run,
    )
    settings_rendezvous_stop_hook = _merge_hook_entry(
        target,
        event="Stop",
        entry=_RENDEZVOUS_STOP_ENTRY,
        needle=_RENDEZVOUS_STOP_NEEDLE,
        dry_run=dry_run,
    )

    # Register the registry-projected MCP server (hpc-agent mcp-serve) as the
    # preferred shell-free block-invocation surface (design §3) — additive +
    # idempotent into .claude.json's mcpServers, never clobbering other servers.
    mcp_server = _register_mcp_server(target, dry_run=dry_run)

    # Grant Skill(<name>) for every installed skill so Claude Code's auto-mode
    # classifier stops silently denying the first /submit-hpc → Skill(hpc-submit)
    # call. Same additive + idempotent + skip-unparseable contract as the hook
    # merge above.
    settings_permissions = _merge_skill_permissions(target, sorted(skills), dry_run=dry_run)

    return {
        "claude_dir": str(target),
        "commands_installed": sorted(commands),
        "skills_installed": sorted(skills),
        "agents_installed": sorted(agents),
        "cleared_collisions": cleared,
        "settings_hook": settings_hook,
        "settings_stop_hook": settings_stop_hook,
        "settings_rendezvous_hook": settings_rendezvous_hook,
        "settings_rendezvous_stop_hook": settings_rendezvous_stop_hook,
        "settings_permissions": settings_permissions,
        "mcp_server": mcp_server,
        "wrote": not dry_run,
    }
