"""``hpc-agent`` CLI entry point — public re-export.

The orchestrator and argparse tree live in :mod:`hpc_agent.cli.dispatch`.
The ``hpc-agent`` console script (``pyproject.toml [project.scripts]``)
targets that module directly; this module exists as a stable
``from hpc_agent.cli import main`` alias for external callers.
"""

from __future__ import annotations

from hpc_agent.cli.dispatch import main

__all__ = ["main"]
