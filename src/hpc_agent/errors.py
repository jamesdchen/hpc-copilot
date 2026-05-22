"""Typed exception hierarchy for the atomic-ops layer.

Both surfaces (slash commands and CLI) catch these and re-present:
- CLI maps to a JSON envelope: ``{"ok": false, "error_code": ..., "retry_safe": ...}``
- Slash commands let them propagate; Claude Code formats for the human

Adding new error_code values is a breaking change; bump the package version.
The full enum is documented in ``docs/reference/cli-spec.md``.
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
    "ClusterPartiallyDegraded",
    "SchemaIncompat",
    "Preempted",
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
        "Run `hpc-agent preflight` to diagnose."
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
        "/submit or `hpc-agent submit`."
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
    remediation = "Run `hpc-agent clusters list` to see configured clusters."


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
        "<remote_path>/logs/ for per-task stderr if the resubmit "
        "doesn't produce the expected output."
    )


class ClusterPartiallyDegraded(HpcError):
    """One or more cluster-side data sources were unreachable but the
    operation succeeded with partial data.

    Carries a ``partial_errors`` list attribute of ``{code, detail}``
    dicts so the agent_cli can surface the per-source failures to the
    envelope's top-level ``partial_errors`` key. The operation that
    raises this still set ok:true cluster-side; the exception is the
    typed channel for surfacing what was missed.

    Retry-safe because the typical cause is a transient scheduler
    daemon stall (qhost, sacct).
    """

    error_code = "cluster_partially_degraded"
    retry_safe = True
    category = "cluster"
    remediation = (
        "One or more node-state queries (qhost, scontrol, sacct, qacct) "
        "timed out or returned malformed output. The result is usable but "
        "may under-count co-tenants or stale-bucket nodes. Re-run after a "
        "short delay if planning quality matters."
    )

    def __init__(
        self, message: str, *, partial_errors: list[dict[str, str]] | None = None, **kwargs
    ):
        super().__init__(message, **kwargs)
        self.partial_errors: list[dict[str, str]] = list(partial_errors or [])


class Preempted(HpcError):
    """A task or run was preempted by the scheduler.

    Surfaces from the agent envelope when the cluster-side dispatcher
    exited 130 (POSIX preempted) after trapping SIGTERM, or when the
    per-task sidecar carries a ``preempt: {at, grace_sec}`` block. The
    campus user got bumped by higher-priority work, not failed; the
    harness can resubmit cleanly without redoing already-completed
    work (dispatch.py's metrics.json idempotency skip handles that).
    """

    error_code = "preempted"
    retry_safe = True
    category = "cluster"
    remediation = (
        "Job was preempted by the scheduler (higher-priority work "
        "claimed the resources). Resubmit when ready; agent harnesses "
        "can resubmit immediately."
    )


class SchemaIncompat(HpcError):
    """An on-disk JSON file declared a ``schema_version`` outside our
    supported range for that domain.

    Raised by :func:`hpc_agent._internal.version.compatibility_check` so the
    five readers in the codebase (session, blacklist, runtime_prior,
    calibration prediction, status rollup, per-run sidecar) all surface
    the same error code.

    Not retry-safe — the file on disk has a shape we cannot read.
    Either the writer is newer than the reader (upgrade the package) or
    the file was hand-edited / from a different repo.
    """

    error_code = "schema_incompat"
    retry_safe = False
    category = "internal"
    remediation = (
        "The on-disk JSON was written by a newer (or older, foreign) "
        "hpc-agent version than this one supports. Upgrade the package "
        "or migrate the file. The supported version set is declared in "
        "``hpc_agent/_internal/version.py:_MANIFEST``."
    )
