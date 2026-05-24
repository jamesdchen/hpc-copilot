"""Back-compat shim for ``from hpc_agent import runner`` callers.

The mapreduce + journal operations the legacy ``runner`` package
bundled were migrated to subject packages in Waves 2-3:

* submit + bookkeeping -> :mod:`hpc_agent.ops.submit.runner`
* monitor (status / reconcile / logs) -> :mod:`hpc_agent.ops.monitor.*`
* aggregate (combine / verify / provenance) -> :mod:`hpc_agent.ops.aggregate.*`
* recover (resubmit + failure clustering) -> :mod:`hpc_agent.ops.recover.*`

A handful of external callers / tests still write
``from hpc_agent import runner`` and reach for ``runner.<symbol>``.
This module re-exports the previous public surface from the new homes
so those callers keep working. New code should import the symbol from
its canonical subject module instead.
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
from hpc_agent.ops.monitor.reconcile import mark_terminal, reconcile
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
    "DEFAULT_AUTO_RETRY_POLICY",
    "annotate_clusters_with_retry_advice",
    "build_job_env",
    "build_provenance",
    "cluster_failures_by_fingerprint",
    "combine_wave",
    "derive_resubmit_request_id",
    "fetch_task_logs",
    "fingerprint_stderr_tail",
    "mark_terminal",
    "reconcile",
    "record_status",
    "resubmit_failed",
    "submit_and_record",
    "verify_combiner_artifact",
    "verify_per_task_outputs",
    "write_remote_provenance",
]
