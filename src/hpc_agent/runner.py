"""Cross-subject primitive bridge + back-compat shim.

Two roles:

1. **Back-compat surface** — external callers and legacy tests still
   write ``from hpc_agent import runner`` and reach for ``runner.<symbol>``.
   The Wave 2-3 reorg migrated those symbols into subject packages:

   * submit + bookkeeping -> :mod:`hpc_agent.ops.submit.runner`
   * monitor (status / reconcile / logs) -> :mod:`hpc_agent.ops.monitor.*`
   * aggregate (combine / verify / provenance) -> :mod:`hpc_agent.ops.aggregate.*`
   * recover (resubmit + failure clustering) -> :mod:`hpc_agent.ops.recover.*`

   This module re-exports the previous public surface from the new homes
   so those callers keep working.

2. **Cross-subject primitive bridge** — when code inside a subject (e.g.
   an atom in ``ops/recover/``) needs to *call* a primitive from another
   subject, the subject-imports lint correctly flags a direct
   ``from hpc_agent.ops.X.Y import ...`` as a cross-subject reach.
   (Workflows post-P5a live at the ``ops/`` and ``meta/`` role roots
   as sibling files, so workflow-to-atom calls no longer need this
   bridge — workflows import atoms directly.)
   The principled escape hatch is to route the call through this module:
   it lives at the package root (not inside any subject) so the lint
   permits the import. Conceptually it mirrors what the registry already
   does at the metadata layer — ``composes=["primitive-name"]`` is the
   declarative form; this module is the callable form.

   Use this only for cross-subject ``@primitive`` calls. Helper-shaped
   shared code still belongs in ``infra/``.
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
from hpc_agent.ops.validate.executor_signatures import validate_executor_signatures
from hpc_agent.ops.validate.input_dataset import validate_input_dataset
from hpc_agent.ops.validate.stochastic_marker import validate_stochastic_marker
from hpc_agent.ops.validate.walltime_against_history import (
    validate_walltime_against_history,
)

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
    "validate_executor_signatures",
    "validate_input_dataset",
    "validate_stochastic_marker",
    "validate_walltime_against_history",
    "verify_combiner_artifact",
    "verify_per_task_outputs",
    "write_remote_provenance",
]
