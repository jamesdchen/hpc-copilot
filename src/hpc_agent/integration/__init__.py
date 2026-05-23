"""Stable constants for integrators composing on top of hpc-agent.

External agent harnesses that drive ``hpc-agent`` from a Bash-style
tool tend to hard-code the same handful of env-var names, lifecycle
states, and error codes. This module surfaces them as Python
constants so harness code can ``from hpc_agent.integration import …``
instead of carrying string literals that drift out of sync.

The names exported here are part of the wire contract; renaming or
removing one is a minor-version bump. Adding is fine.
"""

from __future__ import annotations

# ── dispatcher-side env vars (read by user executors on the cluster) ─────────

RESULT_DIR_ENV = "RESULT_DIR"
"""Per-task working directory. Atomically promoted from `_wip_<task_id>/`
to the final dir on exit-0. Executors write outputs here."""

HPC_KW_PREFIX = "HPC_KW_"
"""Prefix the dispatcher uses to export each kwarg returned by
`tasks.resolve(task_id)`. ``HPC_KW_<KEY>=<value>``, JSON-encoded."""

LOCAL_DATA_DIR_ENV = "LOCAL_DATA_DIR"
"""Optional cluster-side data root. Templates honor it when set;
executors that read shared data files key off it."""

# ── caller-side env vars (set by the integrator's shell) ────────────────────

JOURNAL_DIR_ENV = "HPC_JOURNAL_DIR"
"""Root of the per-experiment journal tree. Defaults to
``~/.claude/hpc/``. Integrators set this to an isolated path so
concurrent harness runs don't share state."""

CLUSTERS_CONFIG_ENV = "HPC_CLUSTERS_CONFIG"
"""Override path to ``clusters.yaml``. When unset, the loader reads
the package-shipped default at ``hpc_agent/config/clusters.yaml``."""

# ── enums ────────────────────────────────────────────────────────────────────

LIFECYCLE_STATES = frozenset(
    {
        "in_flight",
        "complete",
        "failed",
        "timeout",
        "abandoned",
    }
)
"""Every value that may appear in ``data.lifecycle_state`` on a
status / monitor / reconcile envelope. Terminal states are everything
except ``in_flight``."""

ERROR_CODES = frozenset(
    {
        "ssh_unreachable",
        "scheduler_throttled",
        "cluster_timeout",
        "combiner_failed",
        "preempted",
        "cluster_partially_degraded",
        "remote_command_failed",
        "spec_invalid",
        "executor_not_found",
        "cluster_unknown",
        "config_invalid",
        "outputs_missing",
        "journal_corrupt",
        "schema_incompat",
        "precondition_failed",
    }
)
"""The 15 ``error_code`` values an error envelope may carry. The
catch-all ``internal`` code is intentionally NOT included — it
indicates a framework bug or corrupt state, not a stable wire
contract integrators should branch on. The full set with retry-policy
notes lives in ``docs/integrations/CONTRACT.md``."""

__all__ = [
    "CLUSTERS_CONFIG_ENV",
    "ERROR_CODES",
    "HPC_KW_PREFIX",
    "JOURNAL_DIR_ENV",
    "LIFECYCLE_STATES",
    "LOCAL_DATA_DIR_ENV",
    "RESULT_DIR_ENV",
]
