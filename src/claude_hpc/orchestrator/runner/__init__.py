"""Bundled mapreduce + journal operations.

Each public function pairs a cluster-mutating mapreduce primitive with the
corresponding journal update, so slash commands can't accidentally do one
without the other (the failure mode that motivated this module).

``claude_hpc._internal.session`` stays pure-IO; this package is the seam where
SSH calls and journal writes meet.
"""

from __future__ import annotations

from claude_hpc._internal._time import utcnow_iso as _utcnow_iso
from claude_hpc.orchestrator.runner._ssh import (
    _parse_remote_json,
    _split_ssh_target,
)
from claude_hpc.orchestrator.runner.aggregate import (
    _read_remote_sidecar,
    _wave_task_ids,
    build_provenance,
    verify_combiner_artifact,
    verify_per_task_outputs,
    write_remote_provenance,
)
from claude_hpc.orchestrator.runner.combine import combine_wave
from claude_hpc.orchestrator.runner.failures import (
    _FAILURE_CATEGORY_PATTERNS,
    _FINGERPRINT_NOISE,
    DEFAULT_AUTO_RETRY_POLICY,
    _categorize,
    annotate_clusters_with_retry_advice,
    cluster_failures_by_fingerprint,
    fingerprint_stderr_tail,
)
from claude_hpc.orchestrator.runner.logs import fetch_task_logs
from claude_hpc.orchestrator.runner.reconcile import (
    _ssh_alive_job_ids,
    _ssh_list_combined_waves,
    mark_terminal,
    reconcile,
)
from claude_hpc.orchestrator.runner.resubmit import (
    derive_resubmit_request_id,
    resubmit_failed,
)
from claude_hpc.orchestrator.runner.status import _ssh_status_report, record_status
from claude_hpc.orchestrator.runner.submit import build_job_env, submit_and_record

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
