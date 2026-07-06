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
    "SshCircuitOpen",
    "SshSlotWaitTimeout",
    "SchedulerThrottled",
    "SpecInvalid",
    "ExecutorNotFound",
    "ClusterUnknown",
    "JournalCorrupt",
    "PreconditionFailed",
    "RemoteCommandFailed",
    "ConfigInvalid",
    "CombinerFailed",
    "ClusterTimeout",
    "OutputsMissing",
    "ClusterPartiallyDegraded",
    "SchemaIncompat",
    "Preempted",
    "AlreadyInFlight",
    "SiblingRunLive",
    "SubmissionIncomplete",
    "SpawnWorkerDied",
    "StructuredOutputError",
    "ModelEndpointError",
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


class SshCircuitOpen(HpcError):
    """Per-host SSH circuit breaker is open — attempts to this host fail fast.

    Raised by :mod:`hpc_agent.infra.ssh_circuit` at the ssh seam after
    :data:`~hpc_agent.infra.ssh_circuit.CIRCUIT_THRESHOLD` consecutive
    connection-level failures to one host (connect/banner timeouts,
    connection refused/reset — NOT auth failures or remote-command exits).
    Ban-hammer protection: a fleet of workers/retries collectively hammering
    a host with half-open connections is what gets the source IP banned by
    the cluster's intrusion filter (2026-07-04 incident), so once the
    breaker is open every further attempt is refused locally until the
    cooldown ends and a single half-open probe succeeds.

    ``retry_safe=False`` on purpose: an immediate retry against an open
    circuit is exactly the behavior the breaker exists to stop. The message
    carries the cooldown deadline and the per-host override.
    """

    error_code = "ssh_circuit_open"
    retry_safe = False
    category = "network"

    #: Structured context the raiser (``ssh_circuit._open_error``) attaches:
    #: the host whose circuit is open and the epoch second its cooldown ends.
    #: Class-level defaults keep bare construction (tests, older sites) valid;
    #: consumers must treat ``deadline is None`` as "unknown — do not wait".
    host: str = ""
    deadline: float | None = None
    remediation = (
        "Wait for the cooldown deadline named in the message (a single probe "
        "then re-checks the host), or verify the host is reachable out-of-band "
        "and set HPC_SSH_CIRCUIT_OVERRIDE=<host> to explicitly bypass the "
        "breaker for that host only. Inspect the recorded failures under "
        "<journal home>/_ssh_circuit/<host>.json."
    )


class SshSlotWaitTimeout(HpcError):
    """Gave up waiting for a per-host SSH connection slot — burst prevention.

    Raised by :mod:`hpc_agent.infra.ssh_slots` when all of a host's
    connection slots (default 2, fleet-wide across processes) stayed held
    for the whole bounded wait window (~120s). This is *local* contention —
    our own fleet is saturating its self-imposed per-host cap — not
    evidence the host is down, so it does NOT count toward the circuit
    breaker. The bound exists so a queue of waiters can never become a new
    wedge class (2026-07-05 proving-run-#4 burst incident).

    Reuses the ``ssh_unreachable`` error_code: adding a new code is a
    breaking envelope change (same posture as :class:`SiblingRunLive`).
    ``retry_safe=True``: retrying later, after in-flight calls drain, is
    the natural recovery.
    """

    error_code = "ssh_unreachable"
    retry_safe = True
    category = "network"
    remediation = (
        "Too many concurrent SSH calls to one host from this machine's "
        "hpc-agent fleet. Let in-flight calls finish and retry; check for "
        "leaked slot files under <journal home>/_ssh_throttle/ if it "
        "persists, or raise the cap with HPC_SSH_MAX_CONNECTIONS=<n> "
        "(0 disables the limiter)."
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


class PreconditionFailed(HpcError):
    """A workflow step was invoked out of order — a precondition on prior
    on-disk state is not met.

    Raised before any cluster-side work when a flow is called against a
    run that is not in the expected state: ``monitor-flow`` on a run
    that never reached the scheduler (no job ids), or ``aggregate-flow``
    on a run that monitor-flow has not driven to a terminal state.
    Failing loud with this code is deliberate — proceeding would loop
    against nothing or reduce over partial data and report
    plausible-but-wrong metrics.
    """

    error_code = "precondition_failed"
    retry_safe = False
    category = "user"
    remediation = (
        "Run the prior workflow step first (submit-flow before "
        "monitor-flow; monitor-flow to a terminal state before "
        "aggregate-flow). `hpc-agent load-context` reports each run's "
        "actual on-disk state."
    )


class RemoteCommandFailed(HpcError):
    """A remote command returned a non-zero exit code (status reporter, combiner, etc.)."""

    error_code = "remote_command_failed"
    retry_safe = False
    category = "cluster"
    remediation = "Check the cluster-side stderr captured in the exception message."

    def __init__(
        self,
        message: str,
        *,
        remediation: str | None = None,
        returncode: int | None = None,
    ) -> None:
        # Carry the remote command's exit code as a first-class attribute so a
        # caller can split a DETERMINISTIC broken-env failure (rc 126 "not
        # executable" / 127 "command not found" — the run's python/conda env is
        # absent and no amount of waiting heals it) from a transient transport
        # failure WITHOUT string-parsing the message (see
        # ``ops.verify_canary._classify_poll_failure``). ``None`` when the raiser
        # had no exit code to attach (a parse failure before the child ran, or a
        # legacy call site that predates this kwarg).
        super().__init__(message, remediation=remediation)
        self.returncode = returncode


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
    dicts so the CLI dispatcher can surface the per-source failures to the
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

    Raised by :func:`hpc_agent._kernel.extension.version.compatibility_check` so the
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
        "``hpc_agent/_kernel/extension/version.py:_MANIFEST``."
    )


# ── Recovery-registry-backed exceptions ────────────────────────────────────
#
# Three exception classes whose ``remediation`` is sourced from
# :mod:`hpc_agent.recovery.registry` rather than hand-rolled here. New
# remediation menus land in the registry; this layer reads. The error_code
# stays a coarse envelope category (``spec_invalid`` / ``internal``); the
# fine-grained kind ("already_in_flight" etc.) is the registry key.
#
# Per-instance ``remediation=`` overrides still work — the registry value
# is the default, not a clamp.


def _registry_remediation(kind: str) -> str:
    """Lazily render a recovery menu by ``kind``.

    Imported at call time to avoid a top-level import cycle between
    ``hpc_agent.errors`` and ``hpc_agent.recovery.registry`` (the latter
    has no reverse dep today, but the cycle would land the first time
    the registry catches an :class:`HpcError` to surface).
    """
    from hpc_agent.recovery.registry import remediation_for

    return remediation_for(kind)


class AlreadyInFlight(HpcError):
    """A prior run for this cmd_sha is recorded as in_flight in the journal
    AND reconcile confirms the cluster agrees it is still running.

    Distinct from a stale journal entry (which reconcile clears to
    ``abandoned`` — the submit proceeds) and from a network-unreachable
    cluster (``unable_to_verify`` — the submit refuses without claiming
    abandoned). See ``hpc-submit/SKILL.md`` Step 1b for the full branch.
    """

    error_code = "spec_invalid"
    retry_safe = False
    category = "user"

    def __init__(
        self,
        message: str,
        *,
        remediation: str | None = None,
        run_id: str | None = None,
        scheduler: str | None = None,
        experiment_dir: str | None = None,
    ) -> None:
        if remediation is None:
            placeholders = {
                k: v
                for k, v in {
                    "run_id": run_id,
                    "scheduler": scheduler,
                    "experiment_dir": experiment_dir,
                }.items()
                if v is not None
            }
            from hpc_agent.recovery.registry import remediation_for

            remediation = remediation_for("already_in_flight", placeholders=placeholders)
        super().__init__(message, remediation=remediation)


class SiblingRunLive(HpcError):
    """A SIBLING prior run_id — another run in this experiment's journal with
    the SAME code identity (cmd_sha / node_sha) — is still live (``in_flight``,
    possibly with a live detached-worker lease), and the submit under a NEW
    run_id did not name it in ``supersedes``.

    The supersession conduct gate (proving run #4, findings e/g/h): a fresh
    run_id must never be an escape hatch from the single-lease / provenance
    rules — when a scope's history is inconvenient, hopping to a new run_id
    would otherwise make every gate forget. Distinct from
    :class:`AlreadyInFlight` (same run_id replayed): here the run_id is NEW
    but the code identity matches a live prior attempt, so the submit must
    either close the sibling first (``hpc-agent kill --run-id <old>`` /
    reconcile to terminal) or carry an explicit ``supersedes: "<old>"`` field,
    which journals the old→new link and triggers closure of the old attempt.
    """

    # Reuses the ``spec_invalid`` code (same posture as ``AlreadyInFlight``):
    # adding a new error_code value is a breaking envelope change.
    error_code = "spec_invalid"
    retry_safe = False
    category = "user"


class SubmissionIncomplete(HpcError):
    """The qsub/sbatch call structurally succeeded but cluster-side init
    crashed before the sidecar got fully populated — the run record has
    no ``job_ids``, so scheduler state cannot be polled.

    Distinct from ``abandoned`` (job_ids existed but no longer live) and
    from a network-unreachable cluster. The open ``verify-canary`` gap
    (SESSION_HANDOFF.md "Still open") — today the verifier silently
    classifies this as "abandoned"; this exception is the typed channel
    that makes the distinction observable.
    """

    error_code = "spec_invalid"
    retry_safe = False
    category = "user"

    def __init__(
        self,
        message: str,
        *,
        remediation: str | None = None,
        run_id: str | None = None,
        experiment_dir: str | None = None,
        ssh_target: str | None = None,
        remote_path: str | None = None,
    ) -> None:
        if remediation is None:
            placeholders = {
                k: v
                for k, v in {
                    "run_id": run_id,
                    "experiment_dir": experiment_dir,
                    "ssh_target": ssh_target,
                    "remote_path": remote_path,
                }.items()
                if v is not None
            }
            remediation = _registry_remediation_with_placeholders(
                "submission_incomplete", placeholders
            )
        super().__init__(message, remediation=remediation)


class SpawnWorkerDied(HpcError):
    """The spawned ``claude -p --bare`` worker exited 1 before emitting a
    valid report.

    Typically a credential or quota failure the worker hit but the parent
    session does not (e.g. workspace API key over quota while the
    operator's OAuth session is fine — see commit ``29fbac9f``).
    """

    error_code = "internal"
    retry_safe = True
    category = "internal"

    def __init__(self, message: str, *, remediation: str | None = None) -> None:
        if remediation is None:
            remediation = _registry_remediation("spawn_worker_died")
        super().__init__(message, remediation=remediation)


class StructuredOutputError(HpcError):
    """A raw model completion failed to yield a valid structured object.

    Raised by :func:`hpc_agent._kernel.lifecycle.structured.structured`
    after the parse-validate-repair budget is exhausted: every attempt
    either emitted no JSON object, failed the target Pydantic model's
    validation, or was rejected by the caller's ``post_validate`` hook.

    Classed as an internal, retry-safe failure: a malformed completion
    after the repair budget is the model boundary misbehaving (not the
    caller's input), and re-running the funnel with a fresh sample is the
    natural recovery — the same posture as :class:`SpawnWorkerDied` for
    the spawned-worker floor.
    """

    error_code = "internal"
    retry_safe = True
    category = "internal"


class ModelEndpointError(HpcError):
    """A raw model-call transport / response failure at the ChatModel boundary.

    Raised by the OpenAI-compatible adapter
    (:mod:`hpc_agent._kernel.lifecycle.chat_models.openai_compat`) when the
    configured ``HPC_AGENT_MODEL`` endpoint cannot be reached, returns a
    non-2xx status, or returns a body that is not a usable chat-completions
    envelope (non-JSON, or no message content to read). Distinct from
    :class:`StructuredOutputError`, which is a *valid* completion that failed
    the schema / ``post_validate`` floor — this is a failure to obtain a
    completion at all, so it propagates out of
    :func:`hpc_agent._kernel.lifecycle.structured.structured` uncaught (the
    floor only repairs validation failures).

    Retry-safe, ``network`` category: a transient endpoint blip or outage is
    the typical cause, and re-running the funnel resamples.
    """

    error_code = "model_endpoint_error"
    retry_safe = True
    category = "network"
    remediation = (
        "Verify HPC_AGENT_MODEL_BASE_URL is reachable and HPC_AGENT_MODEL_NAME "
        "/ the API key are correct for the endpoint. A 4xx is usually a bad key "
        "or an unsupported response_format; a 5xx or connection error is a "
        "transient outage — retry."
    )


def _registry_remediation_with_placeholders(kind: str, placeholders: dict[str, str]) -> str:
    """Helper sibling of :func:`_registry_remediation` for the
    placeholder-substitution path. Kept separate so the no-placeholders
    call (the common case) doesn't pay the dict-construction cost.
    """
    from hpc_agent.recovery.registry import remediation_for

    return remediation_for(kind, placeholders=placeholders)
