"""Lifecycle / status / failure-category vocabularies.

Pre-B2 the codebase had **four** scattered string vocabularies that
drifted independently — every CHANGELOG-grade bug we shipped in this
area boiled down to two of those vocabularies disagreeing about a
single value:

* ``hpc_agent.state.session.RunRecord.status`` — journal record status
  (set literal ``{"complete", "failed", "abandoned"}`` plus
  ``"in_flight"``).
* ``hpc_agent.ops.monitor_flow``'s ``lifecycle_state`` envelope
  field — workflow state including ``"timeout"``.
* ``hpc_agent.models.mapreduce.reduce.status``'s per-task status strings.
* ``hpc_agent.runner.cluster_failures_by_fingerprint``'s emitted
  category strings vs ``hpc_agent.agent_cli._VALID_RESUBMIT_CATEGORIES``'s
  accepted set — a real bug class where the classifier could emit a
  category the resubmit path silently rejected.

This module is the single source of truth for all four. Every
StrEnum string-coerces transparently for JSON serialization
(Python 3.11+ ``StrEnum`` semantics: ``str(Foo.BAR) == "bar"``).
"""

from __future__ import annotations

import sys

if sys.version_info >= (3, 11):
    from enum import StrEnum
else:  # pragma: no cover - fallback for Python 3.10
    from enum import Enum

    class StrEnum(str, Enum):  # type: ignore[no-redef]
        """Minimal py3.10 backport of :class:`enum.StrEnum`.

        Provides ``str(Foo.BAR) == "bar"`` and equality with bare
        strings, which is all this module relies on.
        """

        def __str__(self) -> str:
            return str(self.value)


__all__ = [
    "JournalStatus",
    "TERMINAL_STATUSES",
    "LifecycleState",
    "TaskStatus",
    "FailureCategory",
]


class JournalStatus(StrEnum):
    """Status field on :class:`hpc_agent.state.session.RunRecord`.

    ``in_flight`` while the run is being monitored; transitions to one
    of the three terminal values when the workflow ends.
    """

    IN_FLIGHT = "in_flight"
    COMPLETE = "complete"
    FAILED = "failed"
    ABANDONED = "abandoned"


# Convenience set of terminal :class:`JournalStatus` values.
# ``hpc_agent.state.session`` historically exposed this same set as a
# module-level ``TERMINAL_STATUSES = frozenset({...})``. Now derived
# from the StrEnum so it cannot drift.
TERMINAL_STATUSES = frozenset(
    {JournalStatus.COMPLETE, JournalStatus.FAILED, JournalStatus.ABANDONED}
)


class LifecycleState(StrEnum):
    """Workflow envelope ``lifecycle_state`` — what monitor_flow,
    status, and reconcile emit.

    Same four terminal values as :class:`JournalStatus`, plus
    ``timeout`` for "wall-clock budget exceeded; cluster jobs may
    still be running" — that fifth value was monitor_flow-only
    pre-B2; A3 already aligned the schemas.
    """

    IN_FLIGHT = "in_flight"
    COMPLETE = "complete"
    FAILED = "failed"
    ABANDONED = "abandoned"
    TIMEOUT = "timeout"


class TaskStatus(StrEnum):
    """Per-task status reported by :mod:`hpc_agent.models.mapreduce.reduce.status`.

    Distinct from :class:`JournalStatus` — a workflow can be ``in_flight``
    while individual tasks are ``complete``, ``running``, ``pending``,
    or ``failed``; ``unknown`` covers task ids the scheduler has lost
    track of.
    """

    COMPLETE = "complete"
    RUNNING = "running"
    PENDING = "pending"
    FAILED = "failed"
    UNKNOWN = "unknown"


class FailureCategory(StrEnum):
    """Failure-fingerprint vocabulary, shared by the auto-classifier
    in :func:`hpc_agent.runner.cluster_failures_by_fingerprint`
    and the resubmit path's ``--spec.category`` validation in
    :mod:`hpc_agent.agent_cli`.

    Pre-B2 the two sets disagreed asymmetrically:

    * The classifier emitted: ``gpu_oom``, ``system_oom``, ``walltime``,
      ``node_failure``, ``import_error``, ``file_not_found``,
      ``permission_denied``, ``disk_full``, ``python_traceback``.
    * The resubmit path accepted: ``gpu_oom``, ``system_oom``, ``segv``,
      ``walltime``, ``node_failure``, ``queue_stall``, ``code_bug``,
      ``unknown``.

    Five classifier emissions were silently rejected at resubmit. The
    A4 fix landed the union; this enum is the canonical home so the
    drift class cannot recur.
    """

    # Classifier-emitted + resubmit-accepted overlap.
    GPU_OOM = "gpu_oom"
    SYSTEM_OOM = "system_oom"
    WALLTIME = "walltime"
    NODE_FAILURE = "node_failure"
    # Resubmit-only (human-supplied, no classifier rule).
    SEGV = "segv"
    QUEUE_STALL = "queue_stall"
    CODE_BUG = "code_bug"
    UNKNOWN = "unknown"
    # Classifier-only (post-A4 also accepted by resubmit).
    IMPORT_ERROR = "import_error"
    FILE_NOT_FOUND = "file_not_found"
    PERMISSION_DENIED = "permission_denied"
    DISK_FULL = "disk_full"
    PYTHON_TRACEBACK = "python_traceback"
    # PR-A: cluster preempted the campus user's low-priority job.
    # Bumped, not failed; harness should resubmit cleanly without
    # surfacing a real failure to the user.
    PREEMPTED = "preempted"
