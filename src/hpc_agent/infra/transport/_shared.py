"""Shared substrate for the transport package's leaf submodules.

Holds only the one symbol imported at *import time* by more than one
submodule — the ``_DEFAULT`` timeout sentinel. Keeping it here (rather than
in :mod:`hpc_agent.infra.transport.__init__`) lets ``_combiner`` bind it as a
default-arg value at module load without importing the engine package, which
would be a circular import during the package's own initialization.
"""

from __future__ import annotations

from typing import Any, Final

# Sentinel marker meaning "caller did not specify a timeout". Mirrors the
# one in :mod:`hpc_agent.infra.remote` — both modules expose the same
# ``timeout=`` contract on their public functions and need a distinct
# value from ``None`` (which is the "disable enforcement" escape hatch).
_DEFAULT: Final[Any] = object()
