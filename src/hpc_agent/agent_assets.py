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


def _build_hook_command() -> str:
    """Build a bash-safe hook command targeting the current Python interpreter.

    Claude Code runs ``PostToolUse`` hooks via ``bash -c '<command>'``. Two
    Windows pitfalls the raw ``sys.executable`` walks into:

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
    executable = sys.executable.replace("\\", "/")
    return f"{shlex.quote(executable)} -m hpc_agent._kernel.hooks.skill_return_autofetch"


# The ``PostToolUse`` hook that auto-fetches a sub-skill's return envelope after
# a composed ``Skill(<sub>)`` returns (see
# :mod:`hpc_agent._kernel.hooks.skill_return_autofetch`). install-commands merges
# this entry into ``~/.claude/settings.json``'s ``hooks.PostToolUse`` array,
# additively, idempotently, and self-healing on a stale prior install.
# ``matcher: "Skill"`` scopes it to the Skill tool; the command pipes the
# PostToolUse payload on stdin into the module's ``main``.
_HOOK_COMMAND = _build_hook_command()
_SKILL_RETURN_HOOK_ENTRY: dict[str, Any] = {
    "matcher": "Skill",
    "hooks": [
        {
            "type": "command",
            "command": _HOOK_COMMAND,
        }
    ],
}


def DEFAULT_CLAUDE_DIR() -> Path:
    """Return ``~/.claude`` (does not create the directory)."""
    return Path.home() / ".claude"


def _find_hook_entry_index(post_tool_use: list[Any]) -> int | None:
    """Return the index of the existing autofetch entry, or ``None`` if absent.

    Match key: any ``PostToolUse`` entry whose ``hooks`` list contains a
    ``command`` hook invoking ``hpc_agent._kernel.hooks.skill_return_autofetch``.
    We match on the module path (not the full command string) so a re-run from
    a different ``sys.executable`` — moved venv, **or an upgrade that fixes
    the command encoding** — still finds the existing entry instead of
    appending a duplicate.
    """
    needle = "hpc_agent._kernel.hooks.skill_return_autofetch"
    for i, entry in enumerate(post_tool_use):
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


def _merge_skill_return_hook(claude_dir: Path, *, dry_run: bool) -> dict[str, Any]:
    """Additively, idempotently wire the autofetch hook into ``settings.json``.

    Reads ``<claude_dir>/settings.json`` (creating an empty ``{}`` model when it
    is absent or unreadable), appends :data:`_SKILL_RETURN_HOOK_ENTRY` to
    ``hooks.PostToolUse`` unless an equivalent entry is already present, and
    writes the merged settings back (pretty-printed, trailing newline). Every
    other key and every other PostToolUse entry is preserved verbatim — the
    merge only ever *adds* our one entry.

    Returns a small report ``{settings_path, action, wrote}`` where ``action``
    is one of ``"added"`` (appended), ``"updated"`` (a stale entry from an
    earlier install — e.g. a moved venv, or the pre-0.10.10 backslash-encoded
    Windows path that bash mis-interpreted as escapes — replaced in place),
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
    post_tool_use = hooks.get("PostToolUse")
    if not isinstance(post_tool_use, list):
        post_tool_use = []

    existing_idx = _find_hook_entry_index(post_tool_use)
    if existing_idx is not None and post_tool_use[existing_idx] == _SKILL_RETURN_HOOK_ENTRY:
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

    post_tool_use = list(post_tool_use)
    if existing_idx is not None:
        post_tool_use[existing_idx] = _SKILL_RETURN_HOOK_ENTRY
        action = "updated"
    else:
        post_tool_use.append(_SKILL_RETURN_HOOK_ENTRY)
        action = "added"
    hooks["PostToolUse"] = post_tool_use
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

    Sibling of :func:`_merge_skill_return_hook`: same additive + idempotent
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
            "settings_permissions": {"settings_path": "...", "action": "added",
                                     "added": ["Skill(hpc-submit)", ...], "wrote": <bool>},
            "wrote": <bool>,
        }

    ``cleared_collisions`` lists any pre-existing 0-byte files at
    ``<claude>/commands``/``skills``/``agents`` that were silently
    removed before mkdir — see :func:`_resolve_dir_collision`. Non-empty
    collisions still raise :class:`FileExistsError`.

    ``settings_hook`` reports the additive, idempotent merge of the
    skill-return autofetch ``PostToolUse`` hook into
    ``<claude>/settings.json`` — see :func:`_merge_skill_return_hook`. Its
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

    # Wire the skill-return autofetch PostToolUse hook into settings.json —
    # additive + idempotent, never clobbering existing hooks/keys.
    settings_hook = _merge_skill_return_hook(target, dry_run=dry_run)

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
        "settings_permissions": settings_permissions,
        "wrote": not dry_run,
    }
