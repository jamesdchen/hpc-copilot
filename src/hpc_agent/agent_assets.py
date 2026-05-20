"""Install hpc-agent's bundled slash commands and skills into ``~/.claude/``.

The CLI surface is ``hpc-agent install-commands`` and lives in
``agent_cli.py``; this module provides the copy logic so a pip-only
install (no repo checkout) can still wire the agent assets into Claude
Code's user-global config directory.

Both asset trees ship as package data inside the ``slash_commands``
package — ``slash_commands/commands/*.md`` and
``slash_commands/skills/<name>/SKILL.md`` — so they resolve the same
way whether installed from a wheel or run from a checkout.
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


def install_agent_assets(
    *, claude_dir: Path | None = None, dry_run: bool = False
) -> dict[str, Any]:
    """Copy bundled slash commands and skills into *claude_dir*.

    Slash commands land in ``<claude_dir>/commands/`` and skills in
    ``<claude_dir>/skills/<name>/``, overwriting any existing files.
    With ``dry_run=True`` nothing is written — the returned dict still
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
    package = files("slash_commands")

    commands_src = package / "commands"
    commands = sorted(
        entry.name[:-3]
        for entry in commands_src.iterdir()
        if entry.name.endswith(".md")
    )

    skills_src = package / "skills"
    skills = sorted(entry.name for entry in skills_src.iterdir() if entry.is_dir())

    if not dry_run:
        commands_dst = target / "commands"
        commands_dst.mkdir(parents=True, exist_ok=True)
        for entry in commands_src.iterdir():
            if not entry.name.endswith(".md"):
                continue
            with as_file(entry) as real:
                shutil.copy2(real, commands_dst / entry.name)

        skills_dst_root = target / "skills"
        for name in skills:
            skill_dst = skills_dst_root / name
            skill_dst.mkdir(parents=True, exist_ok=True)
            for entry in (skills_src / name).iterdir():
                if entry.is_dir():
                    continue
                with as_file(entry) as real:
                    shutil.copy2(real, skill_dst / entry.name)

    return {
        "claude_dir": str(target),
        "commands_installed": commands,
        "skills_installed": skills,
        "wrote": not dry_run,
    }
