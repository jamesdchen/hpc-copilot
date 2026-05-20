"""Bundled mapreduce + journal operations.

Each public function pairs a cluster-mutating mapreduce primitive with the
corresponding journal update, so slash commands can't accidentally do one
without the other (the failure mode that motivated this module).

``hpc_agent._internal.session`` stays pure-IO; this package is the seam where
SSH calls and journal writes meet.
"""

from __future__ import annotations

# Private symbols (leading underscore) are intentional re-exports for
# back-compat with external callers and tests that imported them from the
# pre-split flat module. Keep the noqa.
from hpc_agent._internal.time import utcnow_iso as _utcnow_iso  # noqa: F401
from hpc_agent.runner._ssh import (
    _parse_remote_json,  # noqa: F401
)
from hpc_agent.runner.aggregate import (
    _read_remote_sidecar,  # noqa: F401
    _wave_task_ids,  # noqa: F401
    build_provenance,
    verify_combiner_artifact,
    verify_per_task_outputs,
    write_remote_provenance,
)
from hpc_agent.runner.combine import combine_wave
from hpc_agent.runner.failures import (
    _FAILURE_CATEGORY_PATTERNS,  # noqa: F401
    _FINGERPRINT_NOISE,  # noqa: F401
    DEFAULT_AUTO_RETRY_POLICY,
    _categorize,  # noqa: F401
    annotate_clusters_with_retry_advice,
    cluster_failures_by_fingerprint,
    fingerprint_stderr_tail,
)
from hpc_agent.runner.logs import fetch_task_logs
from hpc_agent.runner.reconcile import (
    _ssh_alive_job_ids,  # noqa: F401
    _ssh_list_combined_waves,  # noqa: F401
    mark_terminal,
    reconcile,
)
from hpc_agent.runner.resubmit import (
    derive_resubmit_request_id,
    resubmit_failed,
)
from hpc_agent.runner.status import (
    _ssh_status_report,  # noqa: F401
    record_status,
)
from hpc_agent.runner.submit import build_job_env, submit_and_record

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
