"""The halo-aware series loader — ``hpc_agent.experiment_kit.series`` import path.

The implementation lives in :mod:`hpc_agent.experiment_kit._runtime`, the
self-contained stdlib-only cluster runtime that
:func:`hpc_agent.experiment_kit.export_notebook` inlines verbatim. This module
keeps the historical ``hpc_agent.experiment_kit.series`` import path stable
and is a pure re-export.
"""

from __future__ import annotations

from hpc_agent.experiment_kit._runtime import (
    SeriesNotConfigured,
    SliceSpec,
    activate_slice,
    current_slice,
    deactivate_slice,
    load_series,
    set_series_loader,
    trim_emission,
)

__all__ = [
    "SliceSpec",
    "SeriesNotConfigured",
    "load_series",
    "set_series_loader",
    "current_slice",
    "trim_emission",
    "activate_slice",
    "deactivate_slice",
]
