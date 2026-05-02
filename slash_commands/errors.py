"""Typed exception hierarchy for the atomic-ops layer.

Both surfaces (slash commands and CLI) catch these and re-present:
- CLI maps to a JSON envelope: ``{"ok": false, "error_code": ..., "retry_safe": ...}``
- Slash commands let them propagate; Claude Code formats for the human

Adding new error_code values is a breaking change; bump the package version.
The full enum is documented in ``docs/cli-spec.md``.
"""

from __future__ import annotations

__all__ = [
    "HpcError",
    "SshUnreachable",
    "SchedulerThrottled",
    "SpecInvalid",
    "ExecutorNotFound",
    "ClusterUnknown",
    "JournalCorrupt",
    "RemoteCommandFailed",
    "ConfigInvalid",
    "CombinerFailed",
    "ClusterTimeout",
    "OutputsMissing",
]


class HpcError(Exception):
    """Base for all classified errors in the atomic-ops layer.

    Subclasses set ``error_code``, ``retry_safe``, ``category``, and
    optionally ``remediation`` as class-level attributes. Instances may
    override ``remediation`` per-call when they have host-specific context.
    """

    error_code: str = "internal"
    retry_safe: bool = False
    category: str = "internal"  # one of: user | cluster | network | internal
    remediation: str | None = None

    def __init__(self, message: str, *, remediation: str | None = None) -> None:
        super().__init__(message)
        if remediation is not None:
            self.remediation = remediation


class SshUnreachable(HpcError):
    """SSH connection failed (refused, timed out, auth failed)."""

    error_code = "ssh_unreachable"
    retry_safe = True
    category = "network"
    remediation = (
        "Verify SSH_AUTH_SOCK is forwarded and ssh-agent has a key for the host. "
        "Run `hpc-mapreduce preflight` to diagnose."
    )


class SchedulerThrottled(HpcError):
    """Scheduler rejected submission due to per-user rate limits or quota."""

    error_code = "scheduler_throttled"
    retry_safe = True
    category = "cluster"
    remediation = "Serialize submissions to this cluster; most schedulers cap at ~1/sec."


class SpecInvalid(HpcError):
    """Run definition is malformed or fails validation.

    Covers the per-run sidecar at ``.hpc/runs/<run_id>.json`` and the
    user's ``.hpc/tasks.py`` — whichever the framework was reading at
    the time of the failure.
    """

    error_code = "spec_invalid"
    retry_safe = False
    category = "user"
    remediation = (
        "Inspect .hpc/tasks.py and .hpc/runs/<run_id>.json; rebuild via "
        "/submit or `hpc-mapreduce submit`."
    )


class ExecutorNotFound(HpcError):
    """Referenced executor file does not exist or is not a valid executor."""

    error_code = "executor_not_found"
    retry_safe = False
    category = "user"
    remediation = "Check the executor path exists and matches `discover_executors` heuristics."


class ClusterUnknown(HpcError):
    """Cluster name is not defined in the active clusters.yaml."""

    error_code = "cluster_unknown"
    retry_safe = False
    category = "user"
    remediation = "Run `hpc-mapreduce clusters list` to see configured clusters."


class JournalCorrupt(HpcError):
    """Per-run journal file is unreadable or schema version mismatched."""

    error_code = "journal_corrupt"
    retry_safe = False
    category = "internal"
    remediation = (
        "Inspect the journal file under $HPC_JOURNAL_DIR (or ~/.claude/hpc/); "
        "delete the bad record if you don't need to recover it."
    )


class RemoteCommandFailed(HpcError):
    """A remote command returned a non-zero exit code (status reporter, combiner, etc.)."""

    error_code = "remote_command_failed"
    retry_safe = False
    category = "cluster"
    remediation = "Check the cluster-side stderr captured in the exception message."


class ConfigInvalid(HpcError):
    """clusters.yaml is malformed."""

    error_code = "config_invalid"
    retry_safe = False
    category = "user"
    remediation = "Validate clusters.yaml against the schema published with the package."


class CombinerFailed(HpcError):
    """Per-wave combiner returned non-zero on the cluster."""

    error_code = "combiner_failed"
    retry_safe = True
    category = "cluster"
    remediation = (
        "Inspect the stderr_tail in the JSON payload to find which task's "
        "metrics.json was missing or malformed; resubmit those tasks and "
        "rerun /aggregate."
    )


class ClusterTimeout(HpcError):
    """A scheduler-side subprocess (qsub/sbatch/sacct) exceeded its timeout."""

    error_code = "cluster_timeout"
    retry_safe = True
    category = "cluster"
    remediation = (
        "The scheduler took too long to respond (likely an NFS stall or a "
        "scheduler outage).  Run the same command again after a short delay; "
        "if the problem persists, check cluster status with the ops team."
    )


class OutputsMissing(HpcError):
    """Per-task output files declared by ``--require-outputs`` are absent.

    Raised by ``aggregate`` when the precondition check fails, before the
    combiner runs.  The aggregator refuses to combine on partial data; the
    caller must resubmit the listed task ids and try again.
    """

    error_code = "outputs_missing"
    retry_safe = True
    category = "cluster"
    remediation = (
        "Resubmit the listed task ids and re-run aggregate.  Inspect "
        "<remote_path>/_hpc_logs/ for per-task stderr if the resubmit "
        "doesn't produce the expected output."
    )
