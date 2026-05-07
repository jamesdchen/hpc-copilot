"""Canonical telemetry sink for the framework.

The codebase has five telemetry surfaces today (CLI envelope on stdout,
``print(..., stderr)`` from cluster-side dispatch / combiner, single-use
:mod:`logging`, single-use :func:`warnings.warn`, and the
``<run_id>.monitor.jsonl`` JSONL stream). They use three different
formats and there is no canonical sink — the JSONL stream is the
closest thing, but its writer is tucked inside :mod:`monitor_flow` and
so the slash-command surface had to inline its own copy of the same
flock-append routine. That inlining is what landed item A9 (the
slash-command surface and monitor_flow racing on un-flocked appends
torn-line bug).

This module is the small extraction A9 implies: one
:func:`record(event, payload, *, sink=...)` entry point that the
in-process callers can use, plus a flock-guarded JSONL writer for the
``monitor.jsonl`` sink so any future caller (e.g. campaign manager,
calibration loop) can tail the same file without re-inventing the
write discipline.

Sinks
-----

* ``"stderr-jsonl"`` — write the record as a JSON line to ``sys.stderr``.
  Use this for cluster-side primitives that must stay stdlib-only and
  cannot import this module; they inline the same shape so a tail-f
  on stderr produces the same JSONL stream.
* ``"monitor-jsonl"`` — append to ``<run_id>.monitor.jsonl`` under
  ``runs_dir(experiment_dir)``. Requires ``run_id`` and
  ``experiment_dir`` in *payload* or as additional arguments. Held
  under an exclusive flock so concurrent writers cannot interleave
  bytes (the A9 invariant).
* ``"none"`` — silently drop. The default when ``HPC_TELEMETRY_SINK``
  is unset.

Why we don't migrate ``dispatch.py`` / ``combiner.py``
-----------------------------------------------------

Those modules execute on the cluster where the framework package is
not installed (they ship as standalone Python files inside the
``.hpc/`` payload). Importing :mod:`claude_hpc._internal.telemetry` would
break that constraint. They keep their existing
``print(..., stderr)`` calls; if a future callsite needs JSONL it
should inline a tiny ``_record`` helper rather than pull in this
module.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


# Environment override. Tests / orchestrators set this to redirect
# telemetry to a known sink. Default is "none" so a stray
# :func:`record` call doesn't pollute stderr in production runs.
_ENV_VAR = "HPC_TELEMETRY_SINK"


@contextlib.contextmanager
def flock_append(target: Path):
    """Yield with an exclusive flock on a sibling ``.lock`` file.

    Convenience wrapper around :func:`claude_hpc._internal._io.advisory_flock`.
    Ensures that all writers to ``<run_id>.monitor.jsonl`` serialize their
    appends. Without flock, a concurrent monitor_flow tick and slash-command
    poll can produce a torn JSON line.

    On Windows / no-fcntl platforms degrades to a no-op so the module
    stays importable; the torn-line risk is documented and acceptable
    for non-production environments.
    """
    from claude_hpc._internal._io import advisory_flock

    with advisory_flock(target.with_suffix(target.suffix + ".lock")):
        yield


def _resolve_sink(explicit: str | None) -> str:
    if explicit is not None:
        return explicit
    return os.environ.get(_ENV_VAR, "none")


def record(
    event: str,
    payload: dict[str, Any],
    *,
    sink: str | None = None,
    monitor_jsonl_path: Path | None = None,
) -> None:
    """Record one telemetry event.

    *event* is a stable machine-readable name (``"tick"``,
    ``"poll"``, ``"campaign_step"``); *payload* is a dict of
    arbitrary JSON-serialisable fields. The serialised line is
    ``{"event": event, **payload}`` — callers pre-add ``run_id``,
    ``tick_id``, etc. to *payload* if useful.

    *sink* selects the destination; ``None`` defers to
    ``HPC_TELEMETRY_SINK`` (default ``"none"``). When
    ``sink == "monitor-jsonl"``, *monitor_jsonl_path* must be provided
    (the resolved path of ``<run_id>.monitor.jsonl``). The append is
    held under an exclusive flock; failures are swallowed so a flaky
    log volume cannot tank the parent operation.
    """
    sink = _resolve_sink(sink)
    if sink == "none":
        return
    line = json.dumps({"event": event, **payload}, sort_keys=True)
    if sink == "stderr-jsonl":
        with contextlib.suppress(OSError):
            print(line, file=sys.stderr, flush=True)
        return
    if sink == "monitor-jsonl":
        if monitor_jsonl_path is None:
            raise ValueError("sink='monitor-jsonl' requires monitor_jsonl_path")
        try:
            with (
                flock_append(monitor_jsonl_path),
                monitor_jsonl_path.open("a", encoding="utf-8") as f,
            ):
                f.write(line + "\n")
        except OSError:
            # Telemetry writes must never crash the parent loop.
            pass
        return
    # Unknown sink — be silent rather than raise; we treat sink names
    # as a contract owned by the caller, not a hard schema.


__all__ = ["flock_append", "record"]
