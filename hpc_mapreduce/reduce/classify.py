"""Classify failed task logs into actionable failure categories.

Categories are consumed by the ``/monitor`` slash command to decide whether to
auto-resubmit, adjust resources, or stop and escalate to the user.  See
``agent/commands/monitor.md`` for the action table keyed on these labels.
"""

from __future__ import annotations

__all__ = ["classify_failure", "CATEGORIES"]

import re

#: Valid return values, ordered roughly by specificity.
CATEGORIES = (
    "gpu_oom",
    "system_oom",
    "walltime",
    "node_failure",
    "queue_stall",
    "code_bug",
    "unknown",
)

# Pre-compiled patterns.  Order matters: the first match wins, so place the most
# specific/actionable patterns first (e.g. GPU OOM before generic "Traceback").
_GPU_OOM = re.compile(
    r"CUDA out of memory|torch\.cuda\.OutOfMemoryError|OutOfMemoryError",
    re.IGNORECASE,
)
_WALLTIME = re.compile(
    r"DUE TO TIME LIMIT|CANCELLED.*TIME LIMIT|Time limit exceeded|walltime",
    re.IGNORECASE,
)
_NODE_FAILURE = re.compile(
    r"NODE_FAIL|NODE FAILURE|slurmstepd:\s*error:\s*\*\*\*\s*NODE|\bEqw\b",
)
_SYSTEM_OOM = re.compile(
    r"\bMemoryError\b"
    r"|Out of memory:\s*Kill(ed)? process"
    r"|oom[-_]killer"
    r"|invoked oom-killer"
    r"|Killed\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_QUEUE_STALL = re.compile(r"queue[_\s-]?stall|stalled in queue", re.IGNORECASE)
_TRACEBACK = re.compile(r"Traceback \(most recent call last\):")


def classify_failure(log_text: str) -> str:
    """Classify a failed task's stderr/log text into a category.

    The ``/monitor`` slash command uses the returned label to decide an action
    (resubmit with more memory, bump walltime, escalate, etc.).  Checks are
    order-sensitive: specific patterns are tested before the catch-all Python
    traceback check so that e.g. a ``torch.cuda.OutOfMemoryError`` traceback
    classifies as ``"gpu_oom"`` rather than ``"code_bug"``.

    Returns one of: ``"gpu_oom"``, ``"system_oom"``, ``"walltime"``,
    ``"node_failure"``, ``"queue_stall"``, ``"code_bug"``, ``"unknown"``.
    """
    if not log_text:
        return "unknown"

    if _GPU_OOM.search(log_text):
        return "gpu_oom"
    if _WALLTIME.search(log_text):
        return "walltime"
    if _NODE_FAILURE.search(log_text):
        return "node_failure"
    if _SYSTEM_OOM.search(log_text):
        return "system_oom"
    if _QUEUE_STALL.search(log_text):
        return "queue_stall"
    if _TRACEBACK.search(log_text):
        return "code_bug"
    return "unknown"
