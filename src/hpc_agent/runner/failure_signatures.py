"""Catalog of (stderr pattern, exit code) -> (error_class, suggested_fix).

Pattern adapted from VASPilot's failure-signatures table. Integrating
agents branch on ``classify()`` to auto-resubmit with adjusted
resources rather than asking the user --- e.g. a CUDA OOM gets an
``increase-mem-per-gpu`` fix suggestion that the campaign loop can
apply automatically.

The catalog is ordered by ``priority`` (descending). The first matching
entry wins. ``priority=100`` are the high-confidence resource-error
patterns (OOM, walltime); ``priority=80`` are the user-error
patterns (import, file_not_found, permission); ``priority=10`` is the
generic Python traceback fallback.

Why a separate module from :func:`hpc_agent.runner.failures._categorize`:
the runner only emits a category string. ``classify()`` returns the
full ``{error_class, suggested_fix, matched_pattern}`` triple so the
caller can both display the error and act on the fix recommendation.
The runner keeps its old ``_categorize`` shape; new callers consume
``classify()`` directly.

The ``error_class`` strings align with
:class:`hpc_agent._internal.lifecycle.FailureCategory` once that StrEnum is
on the branch (B2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

__all__ = ["FailureSignature", "classify", "CATALOG"]


@dataclass(frozen=True)
class FailureSignature:
    """One row of the catalog.

    *priority*: higher = more specific, tried first. Two entries with
    the same priority and overlapping patterns are still deterministic
    because we iterate in the catalog list order.
    """

    error_class: str
    stderr_pattern: re.Pattern[str] | None
    exit_code: int | None
    suggested_fix: dict[str, Any]
    priority: int = 50


CATALOG: list[FailureSignature] = [
    FailureSignature(
        error_class="preempted",
        stderr_pattern=re.compile(
            r"\[hpc-agent\] SIGTERM received; cluster preemption imminent",
        ),
        # Cluster-side dispatch.py exits 130 after trapping SIGTERM —
        # the campus user got bumped by higher-priority work, not
        # failed. The harness should resubmit cleanly.
        exit_code=130,
        suggested_fix={"action": "resubmit-preempted"},
        priority=100,
    ),
    FailureSignature(
        error_class="gpu_oom",
        stderr_pattern=re.compile(
            r"CUDA out of memory|RuntimeError: cuda runtime error.*out of memory|"
            r"torch\.cuda\.OutOfMemoryError",
            re.I,
        ),
        exit_code=None,
        suggested_fix={"action": "increase-mem-per-gpu", "factor": 1.5},
        priority=100,
    ),
    FailureSignature(
        error_class="system_oom",
        stderr_pattern=re.compile(
            r"oom-kill|out of memory.*killed|\bMemoryError\b|killed.*signal 9", re.I
        ),
        exit_code=137,
        suggested_fix={"action": "increase-mem", "factor": 1.5},
        priority=100,
    ),
    FailureSignature(
        error_class="walltime",
        # Scheduler-specific markers only. The bare ``\bwalltime\b`` token
        # and ``signal SIGTERM.*15`` previously included here collide with
        # preemption (SLURM/SGE preemption is delivered via SIGTERM with
        # exit 143). The narrowed set matches the sibling
        # ``failures._FAILURE_CATEGORY_PATTERNS`` so the two classifiers
        # cannot disagree — a preempted task no longer gets
        # ``suggested_fix=increase-walltime`` from this catalog while
        # ``failures.py`` correctly tags it ``preempted``.
        stderr_pattern=re.compile(
            r"DUE TO TIME LIMIT|CANCELLED.*TIME LIMIT|"
            r"wall.?time.*expired|wall.?time.*exceeded|"
            r"Time limit exceeded|h_rt.*exceeded|"
            # SGE qacct prints "qmaster enforced h_rt, h_cpu, or h_vmem
            # limit" when a job is killed for exceeding walltime. This
            # is distinct enough from the bare ``\bwalltime\b`` token
            # (which collided with preemption) to be safe.
            r"qmaster enforced h_rt",
            re.I,
        ),
        exit_code=271,
        suggested_fix={"action": "increase-walltime", "factor": 1.5},
        priority=100,
    ),
    FailureSignature(
        error_class="node_failure",
        # ``NODE FAILURE`` (with a space, not just NODE_FAIL),
        # ``slurmstepd: error: *** NODE`` and SGE's ``Eqw`` error state
        # were folded in from mapreduce/reduce/classify.py during the
        # dedup so the wrapper there can delegate fully to this catalog.
        stderr_pattern=re.compile(
            r"NODE_FAIL|NODE FAILURE|node failed|"
            r"slurmstepd:\s*error:\s*\*\*\*\s*NODE|"
            r"\bEqw\b|"
            r"connection (closed|reset by peer)|"
            r"ssh: connect.*refused",
            re.I,
        ),
        exit_code=None,
        suggested_fix={"action": "retry-on-different-node"},
        priority=90,
    ),
    FailureSignature(
        error_class="file_not_found",
        stderr_pattern=re.compile(r"FileNotFoundError|No such file or directory", re.I),
        exit_code=2,
        suggested_fix={"action": "user-fix-paths"},
        priority=80,
    ),
    FailureSignature(
        error_class="import_error",
        stderr_pattern=re.compile(r"ModuleNotFoundError|ImportError", re.I),
        exit_code=1,
        suggested_fix={"action": "user-fix-deps"},
        priority=80,
    ),
    FailureSignature(
        error_class="permission_denied",
        stderr_pattern=re.compile(r"PermissionError|Permission denied", re.I),
        exit_code=13,
        suggested_fix={"action": "user-fix-permissions"},
        priority=80,
    ),
    FailureSignature(
        error_class="disk_full",
        stderr_pattern=re.compile(r"No space left on device|disk.*full|\bENOSPC\b", re.I),
        exit_code=28,
        suggested_fix={"action": "user-clean-disk"},
        priority=80,
    ),
    FailureSignature(
        error_class="python_traceback",
        stderr_pattern=re.compile(r"^Traceback \(most recent call last\):", re.I | re.M),
        exit_code=1,
        suggested_fix={"action": "user-debug"},
        priority=10,
    ),
]


def classify(stderr: str | None, exit_code: int | None) -> dict[str, Any]:
    """Return ``{error_class, suggested_fix, matched_pattern}``.

    Iterates the catalog in priority order (descending). The first hit
    wins. Returns ``{error_class: "unknown", ...}`` on no match.

    *exit_code* is only used as a tiebreaker --- a ``stderr_pattern``
    match alone is sufficient, since exit codes are noisy on schedulers
    that wrap them (qsub returns 0 even when the inner job dies). The
    exit-code-alone path only fires for priority>=90 entries (resource
    errors) to avoid mis-classifying a generic exit=1 as a python
    traceback.
    """
    text = stderr or ""
    sorted_catalog = sorted(CATALOG, key=lambda s: -s.priority)
    # Two passes so the docstring promise — "stderr_pattern match alone
    # is sufficient" — holds in priority order. The single-pass version
    # let a high-priority exit-only hit win against a lower-priority but
    # actually-matching pattern hit.
    for sig in sorted_catalog:
        if sig.stderr_pattern is not None and sig.stderr_pattern.search(text):
            return {
                "error_class": sig.error_class,
                "suggested_fix": dict(sig.suggested_fix),
                "matched_pattern": sig.stderr_pattern.pattern,
            }
    for sig in sorted_catalog:
        exit_hit = (
            sig.exit_code is not None
            and exit_code is not None
            and int(exit_code) == int(sig.exit_code)
        )
        if exit_hit and sig.priority >= 90:
            return {
                "error_class": sig.error_class,
                "suggested_fix": dict(sig.suggested_fix),
                "matched_pattern": None,
            }
    return {
        "error_class": "unknown",
        "suggested_fix": {"action": "user-debug"},
        "matched_pattern": None,
    }
