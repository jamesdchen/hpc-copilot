"""Deprecation shim — package renamed to ``claude_hpc``.

This file ONLY exists to keep ``from hpc_mapreduce import X`` and
``import hpc_mapreduce`` working for one release. Switch your imports
to ``claude_hpc`` directly; the shim will be removed in a future
release.

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

import warnings

warnings.warn(
    "hpc_mapreduce has been renamed to claude_hpc. "
    "Update your imports; the shim will be removed in a future release.",
    DeprecationWarning,
    stacklevel=2,
)

# Re-export the entire public API surface of claude_hpc. ``import
# hpc_mapreduce`` and ``from hpc_mapreduce import X`` keep working
# because ``claude_hpc.__all__`` is the same set of names.
from claude_hpc import *  # noqa: F401,F403
from claude_hpc import (  # noqa: F401
    _PACKAGE_ROOT,
    __version__,
)
