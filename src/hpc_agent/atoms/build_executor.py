"""``build-executor`` primitive — scaffold a starter executor script.

Drops a minimal Python executor template into ``<output_dir>/<name>.py``
that the agent (or human) then fills in. Refuses to overwrite an existing
file unless explicitly forced — agent-edited executors are easy to wipe
out otherwise.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import hpc_agent
from hpc_agent import errors
from hpc_agent._internal.primitive import SideEffect, primitive

if TYPE_CHECKING:
    from pathlib import Path


@primitive(
    name="build-executor",
    verb="scaffold",
    side_effects=[
        SideEffect(
            "writes-file",
            "<output_dir>/<name>.py (refuses to overwrite without --force)",
        ),
    ],
    idempotent=False,
    error_codes=[errors.SpecInvalid, errors.ConfigInvalid],
    cli="hpc-agent build-executor --name <stem> [--output-dir <dir>] [--type plain] [--force]",
    agent_facing=True,
)
def build_executor(
    *,
    output_dir: Path,
    name: str,
    type: str = "plain",
    force: bool = False,
) -> dict[str, Any]:
    """Scaffold ``<output_dir>/<name>.py`` from the named template.

    Returns ``{path, type, source}``: the absolute path of the written
    file, the template type, and the source path the template was
    copied from. Raises :class:`errors.SpecInvalid` for an unknown
    ``type`` or when the destination exists and ``force`` is False.
    """
    starters = hpc_agent._PACKAGE_ROOT / "mapreduce" / "templates" / "scaffolds"
    template_map = {
        "plain": starters / "executor_template.py",
    }
    if type not in template_map:
        raise errors.SpecInvalid(f"unknown --type {type!r}; choose from {sorted(template_map)}")
    src = template_map[type]
    if not src.exists():
        raise errors.ConfigInvalid(f"template missing on disk: {src}")
    dest = (output_dir / name).with_suffix(".py")
    if dest.exists() and not force:
        raise errors.SpecInvalid(f"refusing to overwrite {dest}; pass --force to overwrite")
    output_dir.mkdir(parents=True, exist_ok=True)
    # Pin UTF-8 — HPC nodes with LC_ALL=C / LANG=POSIX would otherwise
    # decode the UTF-8 template using the locale codec and either raise
    # UnicodeDecodeError or silently corrupt non-ASCII content.
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return {"path": str(dest.resolve()), "type": type, "source": str(src)}


__all__ = ["build_executor"]
