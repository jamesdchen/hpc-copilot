"""``register_run`` — the one decorator a notebook experiment applies.

The implementation lives in :mod:`hpc_agent.experiment_kit._runtime`, the
self-contained stdlib-only cluster runtime that
:func:`hpc_agent.experiment_kit.export_notebook` inlines verbatim into an
exported executor. This module is a pure re-export keeping the
``hpc_agent.experiment_kit.register`` import path stable.

``@register_run`` does two things at import time: it records the run in
a module-level ``_RUNS`` registry, and it injects a ``compute(args)``
wrapper into the defining module — satisfying the hpc-agent executor
contract without the researcher writing any CLI glue. Flag synthesis is
a separate authoring step — see :func:`hpc_agent.experiment_kit.flags_for_run`.
"""

from __future__ import annotations

from hpc_agent.experiment_kit._runtime import RunSpec, register_run, save_artifact

__all__ = ["register_run", "RunSpec", "save_artifact"]
