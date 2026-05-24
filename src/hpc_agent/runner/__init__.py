"""Bundled mapreduce + journal operations.

Each public function pairs a cluster-mutating mapreduce primitive with the
corresponding journal update, so slash commands can't accidentally do one
without the other (the failure mode that motivated this module).

``hpc_agent._internal.session`` stays pure-IO; this package is the seam where
SSH calls and journal writes meet.

Public surface is in ``__all__`` below. Private helpers (underscore-prefixed
names like ``_ssh_status_report``) live on their canonical submodule and
must be imported from there directly. The pre-split monolith re-exported
them from this ``__init__`` so test code could write ``runner._X``; that
leaked private surface across the package boundary and is no longer
supported (post-audit cleanup, 2026-05).

PR 1.5 moved the SSH JSON-parse helper (``_parse_remote_json``) to
``infra.remote.parse_remote_json`` and the failure-category regex
catalog (``_FAILURE_CATEGORY_PATTERNS`` / ``_categorize``) to
``infra.parsing.FAILURE_CATEGORY_PATTERNS`` / ``categorize_failure``.

PR 2.3 (recover subject) moved the failure-clustering / resubmit
orchestration out of ``runner/`` and into
:mod:`hpc_agent.ops.recover` — see
:mod:`hpc_agent.ops.recover.runner_failures` (cluster + retry-advice),
:mod:`hpc_agent.ops.recover.failure_signatures` (signature catalog),
and :mod:`hpc_agent.ops.recover.runner` (``resubmit_failed`` /
``derive_resubmit_request_id``). Their symbols are still re-exported on
this package for back-compat; new callers should import from
``hpc_agent.ops.recover`` directly.

PR 3.1 (monitor subject) moved the polling / reconciliation / log-fetch
pipeline out of ``runner/`` and into :mod:`hpc_agent.ops.monitor` — see
:mod:`hpc_agent.ops.monitor.status` (``record_status``,
``ssh_status_report``), :mod:`hpc_agent.ops.monitor.reconcile`
(``reconcile``, ``mark_terminal``), :mod:`hpc_agent.ops.monitor.logs`
(``fetch_task_logs``), and :mod:`hpc_agent.ops.monitor.update_constraints`
(``update_run_constraints``). Their symbols are still re-exported on
this package for back-compat; new callers should import from
``hpc_agent.ops.monitor`` directly.
"""

from __future__ import annotations

from hpc_agent.ops.aggregate.combine import combine_wave
from hpc_agent.ops.aggregate.runner import (
    build_provenance,
    verify_combiner_artifact,
    verify_per_task_outputs,
    write_remote_provenance,
)
from hpc_agent.ops.monitor.logs import fetch_task_logs
from hpc_agent.ops.monitor.reconcile import (
    mark_terminal,
    reconcile,
)
from hpc_agent.ops.monitor.status import record_status
from hpc_agent.ops.recover.runner import (
    derive_resubmit_request_id,
    resubmit_failed,
)
from hpc_agent.ops.recover.runner_failures import (
    DEFAULT_AUTO_RETRY_POLICY,
    annotate_clusters_with_retry_advice,
    cluster_failures_by_fingerprint,
    fingerprint_stderr_tail,
)
from hpc_agent.ops.submit.runner import build_job_env, submit_and_record

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
