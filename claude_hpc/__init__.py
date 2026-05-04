"""claude_hpc — HPC orchestrator package.

This file is in transition (Step 1-7 of the package reorg). The full
public API surface still lives in ``hpc_mapreduce/__init__.py``; it
will move here in Step 7. For now this file only exposes the
``_PACKAGE_ROOT`` path so callers that need to reach into shipped
data files (templates, schemas, config) can do so via the new package
without going through the legacy alias.
"""

from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent

__all__ = ["_PACKAGE_ROOT"]
