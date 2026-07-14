"""Shared plumbing for the ``<skill>-preflight`` composite primitives.

:class:`SubCall`, :func:`_synth_error_subresult`, and :func:`_run_subprocess`
were duplicated (byte-for-byte, modulo ``SubCall``'s per-skill docstring) across
``status_preflight``, ``aggregate_preflight``, ``submit_preflight``, and
``classify_axis_preflight``. They live here once so the four composites share one
implementation.

This is a PRIVATE role-root module directly under ``ops/`` (leaf name starts
with ``_``): the ``<skill>-preflight`` composites import it explicitly, but it
bears no ``@primitive`` and is skipped by ``_discover_primitive_modules``. It
only reaches DOWN into ``hpc_agent.infra`` — legal under
``scripts/lint_subject_imports.py`` (no subject boundary is crossed).
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from hpc_agent.infra.bounded_subprocess import run_capture_bounded

__all__ = [
    "SubCall",
    "_run_subprocess",
    "_synth_error_subresult",
]


@dataclass(frozen=True)
class SubCall:
    """One sub-call within a ``<skill>-preflight`` composite (name + full argv)."""

    name: str
    argv: list[str]


def _synth_error_subresult(
    *, error_code: str, message: str, category: str, elapsed_sec: float
) -> dict[str, Any]:
    """Build a SubResult whose envelope is a synthesised ErrorEnvelope.

    Used when the sub-call could not emit its own JSON (spawn failure,
    timeout, non-JSON stdout). Matches ErrorEnvelope in envelope.json.
    """
    return {
        "envelope": {
            "ok": False,
            "error_code": error_code,
            "message": message,
            "category": category,
            "retry_safe": False,
        },
        "elapsed_sec": elapsed_sec,
        "ok": False,
    }


def _run_subprocess(call: SubCall, *, timeout_sec: float) -> dict[str, Any]:
    """Run *call.argv* synchronously; return its SubResult dict.

    Captures stdout + stderr; parses stdout as a JSON envelope. Spawn
    failure, timeout, and non-JSON stdout all synthesise a uniform
    ErrorEnvelope so the outer composite can branch consistently.
    """
    started = time.monotonic()
    try:
        proc = run_capture_bounded(call.argv, timeout_sec=timeout_sec)
    except subprocess.TimeoutExpired:
        return _synth_error_subresult(
            error_code="cluster_timeout",
            message=f"{call.name} exceeded {timeout_sec}s timeout",
            category="cluster",
            elapsed_sec=time.monotonic() - started,
        )
    except OSError as exc:
        return _synth_error_subresult(
            error_code="internal",
            message=f"failed to spawn {call.name}: {exc}",
            category="internal",
            elapsed_sec=time.monotonic() - started,
        )

    elapsed_sec = time.monotonic() - started

    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        stderr_tail = (proc.stderr or "")[-400:]
        return {
            "envelope": {
                "ok": False,
                "error_code": "internal",
                "message": (
                    f"{call.name} did not emit a JSON envelope on stdout; "
                    f"stderr tail: {stderr_tail}"
                ),
                "category": "internal",
                "retry_safe": False,
            },
            "elapsed_sec": elapsed_sec,
            "ok": False,
        }

    return {
        "envelope": envelope,
        "elapsed_sec": elapsed_sec,
        "ok": bool(envelope.get("ok", False)),
    }
