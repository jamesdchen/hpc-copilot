"""Classify failed task logs into actionable failure categories.

Categories are consumed by the ``/status`` slash command to decide whether to
auto-resubmit, adjust resources, or stop and escalate to the user.  See
``slash_commands/commands/status.md`` for the action table keyed on these labels.

Implementation note: the bulk of the (stderr -> category) regex table lives in
:mod:`claude_hpc.orchestrator.failure_signatures` (the newer, agent-resubmit
catalog).  This module is a thin wrapper that delegates the shared categories
(gpu_oom, system_oom, walltime, node_failure, code_bug, unknown) to
:func:`failure_signatures.classify` and remaps the result into the older
``CATEGORIES`` vocabulary used by ``/status``.

Two categories are kept locally because they are intentionally absent from the
failure_signatures catalog:

* ``segv`` -- the catalog deliberately omits a SEGV row (the SEGV blacklist
  feature was deleted; ``test_segv_falls_through`` pins this contract).
* ``queue_stall`` -- emitted only by the local ``/monitor-hpc`` reporter,
  never by an actual stderr the catalog would see.

Local checks fire in the same order as before consolidation so the public
behaviour of :func:`classify_failure` is unchanged.
"""

from __future__ import annotations

__all__ = ["classify_failure", "CATEGORIES"]

import re

from claude_hpc.orchestrator.failure_signatures import classify as _classify_signature

#: Valid return values, ordered roughly by specificity.
CATEGORIES = (
    "gpu_oom",
    "system_oom",
    "segv",
    "walltime",
    "node_failure",
    "queue_stall",
    "code_bug",
    "unknown",
)

# Local-only patterns: categories the failure_signatures catalog does NOT
# emit.  Kept here so classify_failure stays a complete classifier without
# expanding the catalog's contract.
_QUEUE_STALL = re.compile(r"queue[_\s-]?stall|stalled in queue", re.IGNORECASE)
# Tagged separately from node_failure -- a SEGV without a Python
# traceback is the strongest "node may be silently degraded" signal,
# which /monitor-hpc surfaces to the user instead of auto-handling.
_SEGV = re.compile(
    r"Segmentation fault"
    r"|SIGSEGV"
    r"|signal\s*(?:11|SEGV)"
    r"|exit\s*-?11\b"
    r"|core dumped",
    re.IGNORECASE,
)

# Map failure_signatures error_class -> /status CATEGORIES vocabulary.
# Only direct, semantically equivalent mappings live here; segv and
# queue_stall are handled by the local regex checks above and are not
# emitted by the signature catalog.
_SIGNATURE_TO_CATEGORY: dict[str, str] = {
    "gpu_oom": "gpu_oom",
    "system_oom": "system_oom",
    "walltime": "walltime",
    "node_failure": "node_failure",
    "python_traceback": "code_bug",
    # Catalog-only error_class values (preempted, file_not_found, import_error,
    # permission_denied, disk_full) collapse to "code_bug" because they all
    # represent a Python-level failure that /status treats as a bug to escalate
    # rather than an infrastructure issue to auto-retry. The pre-dedup
    # classify.py would have classified the same logs as "code_bug" via the
    # generic Traceback fallback.
    "preempted": "code_bug",
    "file_not_found": "code_bug",
    "import_error": "code_bug",
    "permission_denied": "code_bug",
    "disk_full": "code_bug",
}


def classify_failure(log_text: str) -> str:
    """Classify a failed task's stderr/log text into a category.

    The ``/status`` slash command uses the returned label to decide an action
    (resubmit with more memory, bump walltime, escalate, etc.).  Checks are
    order-sensitive: specific patterns are tested before the catch-all Python
    traceback check so that e.g. a ``torch.cuda.OutOfMemoryError`` traceback
    classifies as ``"gpu_oom"`` rather than ``"code_bug"``.

    Returns one of: ``"gpu_oom"``, ``"system_oom"``, ``"segv"``,
    ``"walltime"``, ``"node_failure"``, ``"queue_stall"``, ``"code_bug"``,
    ``"unknown"``.
    """
    if not log_text:
        return "unknown"

    # 1) Delegate to the failure_signatures catalog for the high-priority
    # resource-error categories. Its priority ordering already reproduces
    # the original "gpu_oom > walltime > node_failure > system_oom" sequence
    # via priority=100/100/100/90; widening walltime/node_failure to absorb
    # this module's old regex variants (CANCELLED.*TIME LIMIT, NODE FAILURE,
    # Eqw, ...) was done as part of the dedup so the delegation is lossless.
    sig = _classify_signature(log_text, None)
    new_class = sig["error_class"]
    if new_class in {"gpu_oom", "walltime", "node_failure", "system_oom"}:
        return _SIGNATURE_TO_CATEGORY[new_class]

    # 2) SEGV check before queue_stall and Traceback: a segfault often emits
    # no Python frames and would otherwise fall through to "unknown".
    if _SEGV.search(log_text):
        return "segv"
    if _QUEUE_STALL.search(log_text):
        return "queue_stall"

    # 3) Generic Python traceback (and other Python-level failures the
    # catalog tags more specifically) -> code_bug.
    if new_class in _SIGNATURE_TO_CATEGORY:
        return _SIGNATURE_TO_CATEGORY[new_class]
    return "unknown"
