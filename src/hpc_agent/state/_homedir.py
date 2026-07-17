"""Leaf journal-home resolver for cold-path cache modules (no heavy imports).

``state.run_record.current_homedir`` is the canonical journal-home resolver,
but importing :mod:`hpc_agent.state.run_record` costs ~85ms cold (its
``dataclasses`` + ``inspect`` chain) — a tax the CLI fast-path cache and the
describe cache paid on EVERY dispatch, including cache hits, just to derive a
cache file path. This module resolves the same location without importing
``run_record`` unless it is ALREADY loaded.

Contract (byte-identical to ``current_homedir`` in every observable case):

1. ``HPC_JOURNAL_DIR`` env var (nonempty) wins — same rule as
   ``current_homedir`` step 1, so ``monkeypatch.setenv`` redirection applies
   identically whether or not ``run_record`` is loaded.
2. If ``hpc_agent.state.run_record`` is already in ``sys.modules``, delegate
   to its ``current_homedir()`` — this honors the back-compat
   ``monkeypatch.setattr(run_record, "HPC_HOMEDIR", ...)`` seam: any process
   where that patch exists has necessarily imported ``run_record``, so the
   seam is consulted exactly when it can matter and never paid for otherwise.
3. ``~/.claude/hpc`` — the same default as ``current_homedir`` step 3.

The only behavioral difference from importing ``run_record`` directly is
IMPORT TIMING, never the resolved path: a process that never loads
``run_record`` cannot have patched its ``HPC_HOMEDIR`` attribute, so skipping
the import cannot skip an override.

MIRROR: hpc_agent.state.run_record::current_homedir <-> journal_homedir here
  pinned-by tests/state/test_homedir_leaf.py (semantics parity + no-import)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = ["journal_homedir"]

_RUN_RECORD_MODULE = "hpc_agent.state.run_record"


def journal_homedir() -> Path:
    """Resolve the journal home without importing ``run_record`` cold.

    See the module docstring for the three-step contract and why step 2's
    sys.modules probe is sufficient for the test-patch seam.
    """
    env_val = os.environ.get("HPC_JOURNAL_DIR")
    if env_val is not None and env_val != "":
        return Path(env_val)
    run_record = sys.modules.get(_RUN_RECORD_MODULE)
    if run_record is not None:
        resolved: Path = run_record.current_homedir()
        return resolved
    return Path.home() / ".claude" / "hpc"
