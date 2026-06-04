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

import shutil
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any

__all__ = ["DEFAULT_CLAUDE_DIR", "install_agent_assets"]


def DEFAULT_CLAUDE_DIR() -> Path:
    """Return ``~/.claude`` (does not create the directory)."""
    return Path.home() / ".claude"


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
            "wrote": <bool>,
        }

    ``cleared_collisions`` lists any pre-existing 0-byte files at
    ``<claude>/commands``/``skills``/``agents`` that were silently
    removed before mkdir — see :func:`_resolve_dir_collision`. Non-empty
    collisions still raise :class:`FileExistsError`.
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

    return {
        "claude_dir": str(target),
        "commands_installed": sorted(commands),
        "skills_installed": sorted(skills),
        "agents_installed": sorted(agents),
        "cleared_collisions": cleared,
        "wrote": not dry_run,
    }
