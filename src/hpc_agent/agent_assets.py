"""Install hpc-agent's bundled slash commands and skills into ``~/.claude/``.

The CLI surface is ``hpc-agent install-commands`` and lives in
:mod:`hpc_agent.cli.setup`; this module provides the copy logic so a pip-only
install (no repo checkout) can still wire the agent assets into Claude
Code's user-global config directory.

The core asset trees ship as package data inside the ``slash_commands``
package — ``slash_commands/commands/*.md`` and
``slash_commands/skills/<name>/SKILL.md``. Optional plugins may ship
their own ``commands/`` + ``skills/`` trees via the
``slash_command_assets`` hook on the ``hpc_agent.plugins`` seam; those
are installed *after* the core assets, so a plugin's copy of a skill
overrides the core one of the same name (last writer wins by path).
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


def _install_tree(root: Any, target: Path, *, dry_run: bool) -> tuple[list[str], list[str]]:
    """Copy one ``commands/`` + ``skills/`` asset tree rooted at *root*.

    *root* is any :mod:`importlib.resources` traversable. Returns
    ``(commands, skills)`` — the command stems and skill-directory names
    found. A missing ``commands/`` or ``skills/`` subtree is skipped, so
    a plugin may contribute only one of the two.
    """
    commands: list[str] = []
    skills: list[str] = []

    commands_src = root / "commands"
    if commands_src.is_dir():
        commands_dst = target / "commands"
        if not dry_run and commands_dst.exists() and not commands_dst.is_dir():
            raise FileExistsError(
                f"{commands_dst} exists but is not a directory — "
                "hpc-agent setup needs to install slash commands here. "
                "Move or remove the conflicting file, then re-run."
            )
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
        if not dry_run and skills_dst.exists() and not skills_dst.is_dir():
            raise FileExistsError(
                f"{skills_dst} exists but is not a directory — "
                "hpc-agent setup needs to install skills here. "
                "Move or remove the conflicting file, then re-run."
            )
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

    return commands, skills


def install_agent_assets(
    *, claude_dir: Path | None = None, dry_run: bool = False
) -> dict[str, Any]:
    """Copy bundled slash commands and skills into *claude_dir*.

    Slash commands land in ``<claude_dir>/commands/`` and skills in
    ``<claude_dir>/skills/<name>/``, overwriting any existing files.
    The core ``slash_commands`` assets are installed first; then any
    plugin exposing a ``slash_command_assets`` tree through the
    ``hpc_agent.plugins`` seam is installed over them, so an installed
    plugin's copy of a skill replaces the core one. With
    ``dry_run=True`` nothing is written — the returned dict still
    reports what would have been copied.

    Result shape::

        {
            "claude_dir": "<resolved path>",
            "commands_installed": ["aggregate-hpc", ...],
            "skills_installed": ["hpc-submit", ...],
            "wrote": <bool>,
        }
    """
    target = (claude_dir or DEFAULT_CLAUDE_DIR()).expanduser()

    commands: set[str] = set()
    skills: set[str] = set()

    core_commands, core_skills = _install_tree(files("slash_commands"), target, dry_run=dry_run)
    commands.update(core_commands)
    skills.update(core_skills)

    # Optional plugins overlay their own assets last — a plugin's
    # skills/<name>/ overrides the core copy of the same name.
    from hpc_agent._kernel.registry.plugins import plugin_slash_command_roots

    for root in plugin_slash_command_roots():
        plugin_commands, plugin_skills = _install_tree(root, target, dry_run=dry_run)
        commands.update(plugin_commands)
        skills.update(plugin_skills)

    return {
        "claude_dir": str(target),
        "commands_installed": sorted(commands),
        "skills_installed": sorted(skills),
        "wrote": not dry_run,
    }
