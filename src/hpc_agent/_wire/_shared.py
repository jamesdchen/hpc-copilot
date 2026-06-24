"""Shared Pydantic types reused across multiple wire schemas.

These are the canonical Python definitions of every wire-shared
constraint (run_id format, scheduler enum, lifecycle states, error
codes, etc.). Each consumer model imports and uses these aliases;
``model_json_schema()`` inlines the constraints into the emitted
JSON. Tightening one alias here regenerates every consumer schema
in lock-step, replacing the cross-file ``$ref`` graph that used to
hold these together.

Aliases are deliberately ``Annotated`` rather than custom
``BaseModel`` subclasses so they inline as ``{type: ..., ...}`` in
the emitted schema without introducing a per-model ``$defs`` entry.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, Field, StringConstraints

# ── identifiers ──────────────────────────────────────────────────────────────

# Strict run-identifier shape — used on every run_id field (input AND
# output). Output run_ids are path-validated against this same pattern, so
# strict output validation catches a malformed-id bug instead of emitting it.
# Filesystem-safe:
# alphanumerics, dot, underscore, hyphen.
RunIdStrict = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._\-]+$")]


# A scheduler-issued job id — digit-leading: SGE ``13610902``, SLURM
# ``8570940`` / ``8570940_3``, PBS ``1234.pbs01``. The digit-leading rule is
# the discriminator against prose placeholders an agent might fabricate to
# satisfy a non-empty constraint (empirical 2026-06-11 demo: the orchestrator
# recorded ``job_ids: ["purged-completed"]`` after the real id was lost,
# poisoning the journal with an id no scheduler ever issued).
SchedulerJobId = Annotated[str, StringConstraints(pattern=r"^\d[A-Za-z0-9._+\-]*$")]


# SSH target: ``user@host`` (or OpenSSH alias resolving to the same).
SshTarget = Annotated[str, StringConstraints(pattern=r"^[^@]+@[^@]+$")]

# Campaign identifier. Same character class as RunIdStrict but
# semantically distinct.
CampaignId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._\-]+$")]

# ── waves ────────────────────────────────────────────────────────────────────

# Wave numbers that have been combined into the run's ``_combiner/`` dir.
CombinedWaves = list[int]

# Wave numbers whose combine attempt exhausted retries.
FailedWaves = list[int]

# ── lifecycle ────────────────────────────────────────────────────────────────

# Used by monitor-flow's terminal output. 'complete' = every task reported
# complete. 'failed' = at least one failure with nothing running/pending.
# 'abandoned' = recorded job_ids no longer known to the scheduler. 'timeout'
# = wall-clock budget exceeded; cluster jobs may still be running.
LifecycleStateTerminal = Literal["complete", "failed", "abandoned", "timeout"]

# Used by status / reconcile (point-in-time observation; no 'timeout').
LifecycleStateObservable = Literal["in_flight", "complete", "failed", "abandoned"]

# Used when an observer also surfaces 'timeout' (e.g. status reading a
# sidecar previously marked timeout by monitor-flow).
LifecycleStateObservableWithTimeout = Literal[
    "in_flight",
    "complete",
    "failed",
    "abandoned",
    "timeout",
]

# Reconcile's envelope can additionally report 'unable_to_verify' (#258): the
# cluster alive-check failed (SSH/auth/network), so the run's true state is
# unknown — distinct from a confirmed 'in_flight'. It can also report
# 'no_run_record' (#356): a benign crashed-submit orphan — a valid jobless
# sidecar with no journal record, safe to discard/overwrite (NOT journal_corrupt).
# Reconcile-specific so the observable literal above stays clean for status.
LifecycleStateReconcile = Literal[
    "in_flight",
    "complete",
    "failed",
    "abandoned",
    "timeout",
    "unable_to_verify",
    "no_run_record",
]

# ── infra ────────────────────────────────────────────────────────────────────


def _validate_registered_backend(value: str) -> str:
    """Reject a scheduler/backend name absent from the live backend registry.

    Was a closed ``Literal`` over the four built-in SSH families; the
    orchestrator may now name any registered backend — the four built-ins
    *plus* any installed plugin backend (e.g. the pure-API github-actions
    backend) — so a plugin backend is expressible as a spec everywhere a
    scheduler name is accepted (#337, Class A).

    ``registered_backend_names`` is imported lazily inside the validator: a
    module-level ``_wire → infra.backends`` import would cycle, and pydantic
    never calls this during ``model_json_schema()`` so the cost is paid only at
    validation time. Going through the registry (not a bare class lookup) loads
    a plugin's ``@register`` side effect first, matching
    ``backend_requires_ssh``.
    """
    from hpc_agent.infra.backends import registered_backend_names

    names = registered_backend_names()
    if value not in names:
        raise ValueError(f"unknown backend {value!r}; registered backends: {sorted(names)}")
    return value


# Scheduler driver. The four built-in families — 'sge' (Sun/Univa/Open Grid
# Engine), 'slurm' (Slurm-Workload-Manager), 'pbspro' (PBS Pro / OpenPBS) and
# 'torque' (TORQUE; distinct PBS forks — see KNOWN_FAMILIES) — validate, as does
# any registered plugin backend. The emitted JSON schema widens to a bare
# ``{type: string}`` (no enum): the valid set is install-dependent, and
# membership is enforced at validation time, not by a frozen schema enum.
Scheduler = Annotated[str, AfterValidator(_validate_registered_backend)]

# Cluster-specific GPU label (e.g. 'A100', 'H100', 'L40S'). Semantic checks
# live in inspect_cluster; the schema only enforces non-empty.
GpuType = Annotated[str, Field(min_length=1)]

# ── error envelope ───────────────────────────────────────────────────────────

# Canonical envelope error_code enum. Output schemas that surface error
# codes inside ``data`` (e.g. failures, status, validate) must use this
# alias so every consumer's enum stays byte-equivalent.
ErrorCode = Literal[
    "ssh_unreachable",
    "model_endpoint_error",
    "scheduler_throttled",
    "spec_invalid",
    "executor_not_found",
    "cluster_unknown",
    "journal_corrupt",
    "remote_command_failed",
    "config_invalid",
    "combiner_failed",
    "cluster_timeout",
    "cluster_partially_degraded",
    "outputs_missing",
    "schema_incompat",
    "preempted",
    "precondition_failed",
    "internal",
]

# ── failure categories ───────────────────────────────────────────────────────

# Values returned by ``hpc_agent.execution.mapreduce.reduce.classify.classify_failure``.
# Order mirrors the classifier's specificity ranking (first-match-wins).
# Re-exported from ``classify.py`` so that module's public ``CATEGORIES``
# tuple stays in sync with this Literal automatically.
FailureCategory = Literal[
    "gpu_oom",
    "system_oom",
    "segv",
    "walltime",
    "node_failure",
    "queue_stall",
    "code_bug",
    "unknown",
]

# Values accepted by the ``resubmit`` primitive's ``--spec.category``.
# Must contain every value emitted by the classifier
# (``infra.failure_signatures.CATALOG`` — the single classifier;
# ``ops.recover.runner_failures.cluster_failures_by_fingerprint`` delegates
# to it) — five emissions (``import_error``, ``file_not_found``,
# ``permission_denied``, ``disk_full``, ``python_traceback``) were
# missing from this Literal before this audit pass, so the up-front gate
# accepted them but ``ResubmitSpec`` rejected them later, AFTER the
# cluster qsub already fired. Plus ``"preempted"`` (a scheduler-level
# state, not a stderr-fingerprint match) — the agent may call resubmit
# with category="preempted" when the cluster bumped a campus user. This
# Literal is the SoT for ``ops.recover_flow._VALID_CATEGORIES`` (derived
# via ``typing.get_args``).
FailureCategoryResubmittable = Literal[
    "gpu_oom",
    "system_oom",
    "segv",
    "walltime",
    "node_failure",
    "queue_stall",
    "code_bug",
    "unknown",
    "import_error",
    "file_not_found",
    "permission_denied",
    "disk_full",
    "python_traceback",
    "preempted",
    # Cluster-side environment / executor-shape failures the canary verifier
    # now classifies (see infra/failure_signatures.py). Carried here so
    # the resubmit path does not silently reject a real classifier emission.
    "uv_not_on_path",
    "conda_command_not_found",
    "output_file_required",
    "module_not_found_hpc_agent",
    "undefined_var_expansion",
    # Multi-rank (MPI) failure modes (#293 PR4).
    "mpi_launcher_missing",
    "mpi_pe_invalid",
    "mpi_init_failed",
]

# ── campaign optimization ─────────────────────────────────────────────────────

# Optimization direction for campaign convergence / target checks. Shared by
# the CampaignManifest wire model and the campaign atoms (campaign-advance,
# campaign-converged) so the vocabulary is single-sourced instead of restated
# as inline Literals + argparse choices in each.
OptimizationDirection = Literal["minimize", "maximize"]

# Plateau-detection baseline for campaign convergence. 'all_time_best' fires
# when the recent window fails to beat the all-time prior best ('no new record
# in N iters'); 'prior_window' fires when it fails to beat the prior window of
# equal size ('improvements have stalled').
PlateauMode = Literal["prior_window", "all_time_best"]

# ── runtime ──────────────────────────────────────────────────────────────────

# Optional execution runtime override. Today only ``uv`` is supported.
Runtime = Literal["uv"]

# Submit backend names exposed on the wire. Validated against the live backend
# registry (same rule as ``Scheduler``): the four built-in SSH families resolve
# to the remote-over-ssh variant, while a registered plugin backend (e.g. the
# pure-API github-actions backend, ``requires_ssh=False``) is accepted too —
# the submit path no longer assumes every backend submits across an SSH
# boundary (#337). ``pbspro`` and ``torque`` are the two PBS forks (distinct
# command grammars; see SchedulerProfile).
BackendName = Annotated[str, AfterValidator(_validate_registered_backend)]
