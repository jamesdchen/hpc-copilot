"""Typed task-id coordinate spaces — make the 0-based/1-based boundary explicit.

A submitted array job carries a task identity in two coordinate spaces that
are trivially confusable because both are plain ``int``:

* :data:`HpcTaskId` — the **0-based** *domain* identity. This is what the
  framework and the user's ``tasks.resolve(task_id)`` reason about: the
  dispatcher's ``HPC_TASK_ID`` (``dispatch.py`` enforces ``0 <= id < n``),
  the per-task ``preempt`` marks + result-dir templating, the journal's
  ``retries`` keys, and ``resubmit_flow.failed_task_ids`` / the resubmit plan.
* :data:`ArrayIndex` — the **1-based** *scheduler-edge* coordinate:
  ``SLURM_ARRAY_TASK_ID`` / ``SGE_TASK_ID`` (jobs submit ``--array=1-N``) and
  the ``JobId_N`` task index ``sacct`` / ``qstat`` report back. The array
  templates recover the 0-based id with ``ARRAY_TASK_ID - 1``.

The conversion between the two is a single ``±1`` and this module is its one
home. As of Phase 2 (#301) everything above the scheduler speaks
``HpcTaskId``: the conversion lives at exactly two boundary edges, both routed
through here — the *submit edge* (``resubmit_plan``'s ``task_range`` and
``get_err_log_paths`` / ``stderr_log_path`` build a 1-based ``ArrayIndex`` via
:func:`to_array_index`) and the *ingest edge* (``query_sacct`` / ``query_pbs``
/ ``query_sge`` parse the scheduler's ``JobId_N`` back to ``HpcTaskId`` via
:func:`to_task_id`). No compensating shift survives downstream — a space
mix-up would otherwise be invisible (both are ``int``) and silently resubmit
the *wrong* task. Any new boundary must call these helpers rather than
open-coding ``± 1``.

The ``NewType`` distinction is the load-bearing part: it lets a type checker
flag a function handed an ``ArrayIndex`` where an ``HpcTaskId`` is expected,
turning today's silent off-by-one into a static error. This mirrors the
codebase's existing "encode the invariant in the type system" pattern — c.f.
the :class:`~hpc_agent._kernel.contract.layout.RepoLayout` /
:class:`~hpc_agent._kernel.contract.layout.JournalLayout` split that turned the
``runs_*`` path collision into a type error, and the ``JournalStatus`` /
``FailureCategory`` StrEnums that single-source their vocabularies.

NOTE: ``NewType`` is a *static-only* distinction — at runtime both are ``int``,
so this catches mix-ups under ``mypy``, not at runtime. A frozen value object
would enforce it at runtime too, at the cost of friction at every call site;
the type-checker guarantee is the deliberate first rung (see the Phase-2 issue).
"""

from __future__ import annotations

from typing import NewType

from hpc_agent import errors

__all__ = ["ArrayIndex", "HpcTaskId", "to_array_index", "to_task_id"]

#: 0-based domain task identity (``HPC_TASK_ID``; ``tasks.resolve`` input).
HpcTaskId = NewType("HpcTaskId", int)

#: 1-based scheduler array coordinate (``SLURM_ARRAY_TASK_ID`` / ``JobId_N``).
ArrayIndex = NewType("ArrayIndex", int)


def to_array_index(task_id: HpcTaskId) -> ArrayIndex:
    """Convert a 0-based :data:`HpcTaskId` to its 1-based :data:`ArrayIndex`.

    This is the *submit* edge: a task at ``HPC_TASK_ID 0`` is launched as
    scheduler array index ``1``. Raises :class:`~hpc_agent.errors.SpecInvalid`
    on a negative id (a programming error — task ids are non-negative).
    """
    value = int(task_id)
    if value < 0:
        raise errors.SpecInvalid(f"HpcTaskId must be >= 0; got {value}")
    return ArrayIndex(value + 1)


def to_task_id(array_index: ArrayIndex) -> HpcTaskId:
    """Convert a 1-based :data:`ArrayIndex` to its 0-based :data:`HpcTaskId`.

    This is the *ingest* edge: the scheduler reports array index ``1`` for the
    task whose domain identity is ``HPC_TASK_ID 0``. Raises
    :class:`~hpc_agent.errors.SpecInvalid` on an index ``< 1`` (the scheduler
    array space is 1-based, so ``0`` or negative is malformed).
    """
    value = int(array_index)
    if value < 1:
        raise errors.SpecInvalid(f"ArrayIndex must be >= 1; got {value}")
    return HpcTaskId(value - 1)
