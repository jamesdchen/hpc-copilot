"""``hpc-agent`` CLI entry point.

The ``hpc-agent`` console script flows through
``hpc_agent.agent_cli:main`` (per ``pyproject.toml``), which re-exports
:func:`main` from here. The single source of truth for the orchestrator
and the argparse tree currently lives in ``hpc_agent.agent_cli`` —
this module is the eventual home for both as the modular split
progresses; today it's a thin alias that keeps the package layout
consistent and gives external imports a stable target
(``from hpc_agent.cli import main``).
"""

from __future__ import annotations

from hpc_agent.agent_cli import main

__all__ = ["main"]
