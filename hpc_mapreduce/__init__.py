"""Deprecation shim — package renamed to ``claude_hpc``.

This file ONLY exists to keep ``from hpc_mapreduce import X`` and
``import hpc_mapreduce.X.Y`` working for one release. Switch your
imports to ``claude_hpc`` directly; the shim will be removed in a
future release.

The 4-domain split is laid out under ``claude_hpc/`` as:

* ``claude_hpc.mapreduce`` — dispatch, combiner, metrics_io, reduce/, templates/
* ``claude_hpc.infra``     — backends, ssh/rsync, GPU selection, inspect
* ``claude_hpc.orchestrator`` — submit/monitor/aggregate flow primitives,
  planner, runs, runtime priors, calibration, backfill, throughput,
  constraints, resubmit, stages, discover, failure_signatures,
  campaign_health, validate, campaign/
* ``claude_hpc.forecast``  — queue-wait baseline, DES simulator,
  microstructure features, residual lifetime, state forecast,
  best-submit-window
* ``claude_hpc._internal`` — shared utilities (_io, _time, _version,
  _primitive, idempotency, layout, lifecycle, telemetry)
* ``claude_hpc.atoms``     — CLI-only primitive dispatchers
"""

from __future__ import annotations

import importlib
import sys
import warnings
from importlib.abc import Loader, MetaPathFinder
from importlib.machinery import ModuleSpec

warnings.warn(
    "hpc_mapreduce has been renamed to claude_hpc. "
    "Update your imports; the shim will be removed in a future release.",
    DeprecationWarning,
    stacklevel=2,
)

# Re-export the top-level public API surface. ``from hpc_mapreduce
# import X`` works for any X in ``claude_hpc.__all__``.
from claude_hpc import *  # noqa: F401,F403,E402
from claude_hpc import (  # noqa: F401,E402
    _PACKAGE_ROOT,
    __version__,
)


class _SubmoduleAlias(MetaPathFinder, Loader):
    """Map ``hpc_mapreduce.X`` import requests onto ``claude_hpc.X``.

    Without this, ``from hpc_mapreduce.executor_cli import flag`` would
    raise ``ModuleNotFoundError`` because the shim's ``__init__.py``
    only re-exports the top level — submodule paths aren't aliased by
    ``from claude_hpc import *``. The meta path finder catches any
    ``hpc_mapreduce.<sub>`` request, defers to claude_hpc, and shares
    the same module object so identity-based checks still work.
    """

    _PREFIX = "hpc_mapreduce."

    def find_spec(self, fullname, path, target=None):
        if not fullname.startswith(self._PREFIX) or fullname == "hpc_mapreduce":
            return None
        # Synthesize a spec; create_module returns the real module.
        return ModuleSpec(fullname, self)

    def create_module(self, spec):
        target = "claude_hpc." + spec.name[len(self._PREFIX) :]
        try:
            module = importlib.import_module(target)
        except ImportError:
            return None  # let normal import machinery raise
        # Also expose at the legacy path so future ``import
        # hpc_mapreduce.X`` skips the finder (cheaper).
        sys.modules[spec.name] = module
        return module

    def exec_module(self, module):
        # Module already executed by claude_hpc.X import; nothing to do.
        return None


# Install once. Idempotent: subsequent reloads of the shim won't
# stack multiple finders.
if not any(isinstance(f, _SubmoduleAlias) for f in sys.meta_path):
    sys.meta_path.append(_SubmoduleAlias())
