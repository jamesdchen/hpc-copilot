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
    "ScopeLocked",
    "SourceUnaudited",
    "PackReceiptsMissing",
    "SubmissionIncomplete",
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
    dicts — the typed channel for the *raising operation's handler* to
    surface per-source failures into the SUCCESS envelope's top-level
    ``partial_errors`` key (the operation still succeeded cluster-side).
    Note the generic CLI error path (``_err_from_hpc``) does not read the
    attribute, and the error envelope has no ``partial_errors`` key: a
    caller that lets this exception escape to the dispatcher gets an
    ordinary error envelope and loses the per-source detail.

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


class ScopeLocked(HpcError):
    """A run's evidence-scope is locked — the ONE reduction seam refuses.

    Raised by :func:`hpc_agent.ops.scope_gate.assert_scopes_unlocked` before
    ``aggregate-flow`` does any SSH / combine / reduce work, when the run's
    sidecar carries a scope tag whose most recent lock/unlock decision is a
    ``lock`` (:func:`hpc_agent.state.scopes.is_scope_locked`). A lock is
    deliberate human state — an embargo on a held-out scope, a reserved look —
    so both the interactive aggregate and the automatic terminal harvest refuse
    rather than spend it. There is exactly ONE exit: a human-journaled
    scope-unlock (never a code override).

    Reuses the ``precondition_failed`` error_code (the
    :class:`PreconditionFailed` precedent, and the same widen-avoidance posture
    as :class:`SiblingRunLive`'s ``spec_invalid`` reuse): reducing a locked
    scope is a workflow step invoked against on-disk state that forbids it, and
    adding a new error_code value is a breaking wire-envelope change.
    """

    error_code = "precondition_failed"
    retry_safe = False
    category = "user"
    remediation = (
        "The scope was locked by a human decision. Unlock it deliberately "
        "before reducing — journal a scope-unlock via `hpc-agent "
        "append-decision` (scope_kind='scope', block='scope-unlock', "
        "resolved={'scope_action': 'unlock'}) naming the tag. Locking exists "
        "to reserve a look; there is no code override."
    )

    @classmethod
    def for_tag(cls, tag: str, *, locked_at: str | None = None) -> ScopeLocked:
        """Build the loud message naming *tag*, its lock ts, and the one exit."""
        when = f" at {locked_at}" if locked_at else ""
        return cls(
            f"scope {tag!r} is locked{when} — refusing to reduce a run tagged "
            "with it (reducing a locked scope would spend a reserved look). The "
            "one exit is a human-journaled scope-unlock via append-decision "
            "(scope_kind='scope', block='scope-unlock', "
            "resolved={'scope_action': 'unlock'})."
        )


class SourceUnaudited(HpcError):
    """The submit pipeline refuses an entry point not hash-linked to a CURRENT audit.

    Raised by :func:`hpc_agent.ops.notebook_gate.assert_source_audited` — the
    graduation gate (notebook-audit substrate D8) — at its two synchronous submit
    seats (:mod:`hpc_agent.ops.resolve_submit_inputs` pre-sidecar,
    :mod:`hpc_agent.ops.submit_flow` pre-staging) when the interview opted in via
    an ``audited_source`` block (D7) but one or more REQUIRED (template) sections
    of the audited ``.py`` are not signed at their current hash: unsigned,
    drifted (signed then edited — unsigned by construction, no drift state
    machine), or trust-revoked by a drifted ``linked_sources`` dependency.

    Opt-in and fail-safe by construction: with NO ``audited_source`` block the
    gate is a byte-identical no-op and this never raises (the
    :mod:`hpc_agent.ops.scope_gate` fail-safe posture). It fires ONLY inside the
    opted-in surface.

    Reuses the ``precondition_failed`` error_code (the :class:`ScopeLocked`
    precedent, itself following :class:`PreconditionFailed`, and the same
    widen-avoidance posture as :class:`SiblingRunLive`'s ``spec_invalid`` reuse):
    submitting an un-audited source is a workflow step invoked against on-disk
    state that forbids it, and adding a new error_code value is a breaking
    wire-envelope change. ``retry_safe=False``: a bare retry re-hits the same
    unsigned state; the one exit is a human sign-off (or a re-draft + re-sign) of
    the named sections.
    """

    error_code = "precondition_failed"
    retry_safe = False
    category = "user"
    remediation = (
        "One or more audited sections are unsigned or drifted. Re-sign each named "
        "section at its CURRENT hash via `hpc-agent append-decision` "
        "(scope_kind='notebook', block='notebook-sign-off', "
        "resolved={audit_id, section, section_sha, view_sha}); a section edited "
        "after signing reads unsigned by construction — re-audit and re-sign it. "
        "A drifted linked source (a changed imported dependency) likewise revokes "
        "the section's sign-off. There is no code override."
    )

    @classmethod
    def for_sections(cls, audit_id: str, sections: list[tuple[str, str]]) -> SourceUnaudited:
        """Build the loud message naming each unsigned/drifted section + its status.

        *sections* is a list of ``(slug, status)`` pairs — exactly what the human
        must re-sign. The message names every one so the refusal is actionable
        without a separate status query.
        """
        detail = ", ".join(f"{slug!r} ({status})" for slug, status in sections)
        return cls(
            f"audited source for audit_id {audit_id!r} is not cleared for "
            f"graduation — {len(sections)} required section(s) not signed at "
            f"their current hash: {detail}. Re-sign each at its current hash via "
            "append-decision (scope_kind='notebook', block='notebook-sign-off'); "
            "an edit (or a drifted linked source) after signing reads unsigned by "
            "construction (drift = unsigned)."
        )


class PackReceiptsMissing(HpcError):
    """The submit pipeline refuses an opted-in experiment whose required domain-pack
    receipts are not CURRENT + ``passed``.

    Raised by :func:`hpc_agent.ops.pack_gate.assert_pack_receipts_current` — the
    domain-pack receipt gate (``docs/design/domain-packs.md``, "Receipt naming +
    the gate contract") — at its two synchronous submit seats
    (:mod:`hpc_agent.ops.resolve_submit_inputs` pre-sidecar,
    :mod:`hpc_agent.ops.submit_flow` pre-staging) when the interview opted into a
    ``packs`` block (D7) but one or more caller-authored ``receipt_bindings`` slots
    do not reduce to a CURRENT, ``passed=true`` receipt: ``missing`` (no receipt),
    ``stale`` (the bind or a checked file drifted — drift = unsigned by
    construction), or ``failed`` (the check ran against live content and reported
    ``passed=false``).

    Opt-in and fail-safe by construction: with NO ``packs`` block the gate is a
    byte-identical no-op and this never raises. It fires ONLY inside the opted-in
    surface; a BROKEN setup (a dangling manifest, an unresolvable/unbound pack) is
    a :class:`SpecInvalid` instead — this class is only the uncleared-receipt case
    (the T9 refusal split).

    Reuses the ``precondition_failed`` error_code (the :class:`SourceUnaudited` /
    :class:`ScopeLocked` precedent, itself following :class:`PreconditionFailed`):
    submitting under un-cleared domain standards is a workflow step invoked against
    on-disk state that forbids it, and adding a new error_code value is a breaking
    wire-envelope change. ``retry_safe=False``: a bare retry re-hits the same
    uncleared state; the one exit is to run the pack's own check and record a
    current receipt (or re-bind + re-check on drift).
    """

    error_code = "precondition_failed"
    retry_safe = False
    category = "user"
    #: Structured per-slot remedy — ``[{slot, status, check}]`` — populated by
    #: :meth:`for_slots`. ``check`` is the caller-authored command the driving skill
    #: runs UNPROMPTED to re-earn the slot (``None`` when the caller recorded none).
    #: Default empty; instances built via :meth:`for_slots` set their own.
    remedy: list[dict[str, str | None]] = []  # noqa: RUF012
    remediation = (
        "One or more required pack receipt slots are missing, stale, or failed. "
        "Run the pack's own check and record a current receipt via `hpc-agent "
        "pack-record-receipt` for each named slot; a slot reads stale when the "
        "bind or a checked file drifted — re-bind via `hpc-agent pack-bind`, then "
        "re-check. There is no code override: a code receipt never softens a human "
        "tier, and a human sign-off never fills a code-receipt slot."
    )

    @classmethod
    def for_slots(
        cls,
        slots: list[tuple[str, str]],
        *,
        checks: dict[str, str | None] | None = None,
    ) -> PackReceiptsMissing:
        """Build the loud message naming each uncleared slot + its status.

        *slots* is a list of ``(slot, status)`` pairs (``status`` one of
        ``missing`` / ``stale`` / ``failed``) — exactly what the caller must
        re-receipt. The message names every one so the refusal is actionable
        without a separate ``pack-status`` query.

        *checks* optionally maps a slot slug → its caller-authored check command
        (the receipt/check association recorded on the interview ``receipt_bindings``
        entry). When supplied, the AUTO-REMEDY has already re-sealed + re-bound any
        stale manifest (journaled old→new), so the only step left is caller-side:
        the exact check command(s) ride the refusal as :attr:`remedy`, phrased for
        the driving skill to run UNPROMPTED and retry — zero human turns (core never
        runs the check itself, DP2). The instance attribute :attr:`remedy` carries
        the structured ``[{slot, status, check}]`` list for a harness to consume.
        """
        detail = ", ".join(f"{slot!r} ({status})" for slot, status in slots)
        checks = checks or {}
        remedy: list[dict[str, str | None]] = [
            {"slot": slot, "status": status, "check": checks.get(slot)} for slot, status in slots
        ]
        cmds = [r["check"] for r in remedy if r["check"]]
        remedy_line = ""
        if cmds:
            joined = "; ".join(str(c) for c in cmds)
            remedy_line = (
                " AUTO-REMEDY: any stale manifest was already re-sealed + re-bound "
                "(journaled old→new). Run the caller-side check command(s) to re-earn "
                f"the receipt(s), then retry the submit WITHOUT asking the human: {joined}"
            )
        err = cls(
            f"domain-pack receipts are not cleared for graduation — "
            f"{len(slots)} required slot(s) not current+passed: {detail}. Record a "
            "current receipt for each via `hpc-agent pack-record-receipt`; a stale "
            "slot means the bind or a checked file drifted — re-bind and re-check "
            f"(drift = unsigned by construction).{remedy_line}"
        )
        err.remedy = remedy
        return err


class SubmissionIncomplete(HpcError):
    """The qsub/sbatch call structurally succeeded but cluster-side init
    crashed before the sidecar got fully populated — the run record has
    no ``job_ids``, so scheduler state cannot be polled.

    Distinct from ``abandoned`` (job_ids existed but no longer live) and
    from a network-unreachable cluster. Closes the ``verify-canary``
    ``job_ids in (None, [])`` gap (see
    ``docs/proposals/recovery-registry.md``, the ``submission_incomplete``
    section) — previously the verifier silently classified this as
    "abandoned"; this exception is the typed channel that makes the
    distinction observable.
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


class StructuredOutputError(HpcError):
    """A raw model completion failed to yield a valid structured object.

    Raised by :func:`hpc_agent._kernel.lifecycle.structured.structured`
    after the parse-validate-repair budget is exhausted: every attempt
    either emitted no JSON object, failed the target Pydantic model's
    validation, or was rejected by the caller's ``post_validate`` hook.

    Classed as an internal, retry-safe failure: a malformed completion
    after the repair budget is the model boundary misbehaving (not the
    caller's input), and re-running the funnel with a fresh sample is the
    natural recovery.
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
