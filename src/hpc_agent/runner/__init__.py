"""Bundled mapreduce + journal operations.

Each public function pairs a cluster-mutating mapreduce primitive with the
corresponding journal update, so slash commands can't accidentally do one
without the other (the failure mode that motivated this module).

``hpc_agent._internal.session`` stays pure-IO; this package is the seam where
SSH calls and journal writes meet.

Public surface is in ``__all__`` below. Private helpers (underscore-prefixed
names like ``_ssh_status_report``, ``_categorize``,
``_FAILURE_CATEGORY_PATTERNS``) live on their canonical submodule —
``hpc_agent.runner.status``, ``.failures``, ``.reconcile`` — and
must be imported from there directly. The pre-split monolith re-exported
them from this ``__init__`` so test code could write ``runner._X``; that
leaked private surface across the package boundary and is no longer
supported (post-audit cleanup, 2026-05).

PR 1.5 moved the SSH JSON-parse helper (``_parse_remote_json``) to
``infra.remote.parse_remote_json`` and the failure-category regex
catalog (``_FAILURE_CATEGORY_PATTERNS`` / ``_categorize``) to
``infra.parsing.FAILURE_CATEGORY_PATTERNS`` / ``categorize_failure``;
``runner.failures`` still re-exports the underscore-prefixed names so
existing call sites keep working.
"""

from __future__ import annotations

from hpc_agent.ops.aggregate.combine import combine_wave
from hpc_agent.ops.aggregate.runner import (
    build_provenance,
    verify_combiner_artifact,
    verify_per_task_outputs,
    write_remote_provenance,
)
from hpc_agent.ops.submit.runner import build_job_env, submit_and_record
from hpc_agent.runner.failures import (
    DEFAULT_AUTO_RETRY_POLICY,
    annotate_clusters_with_retry_advice,
    cluster_failures_by_fingerprint,
    fingerprint_stderr_tail,
)
from hpc_agent.runner.logs import fetch_task_logs
from hpc_agent.runner.reconcile import (
    mark_terminal,
    reconcile,
)
from hpc_agent.runner.resubmit import (
    derive_resubmit_request_id,
    resubmit_failed,
)
from hpc_agent.runner.status import record_status

__all__ = [
    "submit_and_record",
    "build_job_env",
    "record_status",
    "combine_wave",
    "resubmit_failed",
    "reconcile",
    "mark_terminal",
    "verify_per_task_outputs",
    "verify_combiner_artifact",
    "build_provenance",
    "write_remote_provenance",
    "fetch_task_logs",
    "cluster_failures_by_fingerprint",
    "fingerprint_stderr_tail",
    "derive_resubmit_request_id",
    "annotate_clusters_with_retry_advice",
    "DEFAULT_AUTO_RETRY_POLICY",
]
