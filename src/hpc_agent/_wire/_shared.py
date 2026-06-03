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

from pydantic import Field, StringConstraints

# ── identifiers ──────────────────────────────────────────────────────────────

# Strict run-identifier shape used on INPUT schemas. Filesystem-safe:
# alphanumerics, dot, underscore, hyphen.
RunIdStrict = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._\-]+$")]

# Loose run-identifier used on OUTPUT schemas — any string. Output schemas
# use this so legacy sidecars validate.
RunIdLoose = str

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

# ── infra ────────────────────────────────────────────────────────────────────

# Scheduler driver. 'sge' covers Sun/Univa/Open Grid Engine variants;
# 'slurm' covers Slurm-Workload-Manager clusters; 'pbspro' covers PBS Pro /
# OpenPBS and 'torque' covers TORQUE (distinct PBS forks — see KNOWN_FAMILIES).
Scheduler = Literal["sge", "slurm", "pbspro", "torque"]

# Cluster-specific GPU label (e.g. 'A100', 'H100', 'L40S'). Semantic checks
# live in inspect_cluster; the schema only enforces non-empty.
GpuType = Annotated[str, Field(min_length=1)]

# ── error envelope ───────────────────────────────────────────────────────────

# Canonical envelope error_code enum. Output schemas that surface error
# codes inside ``data`` (e.g. failures, status, validate) must use this
# alias so every consumer's enum stays byte-equivalent.
ErrorCode = Literal[
    "ssh_unreachable",
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

# Values returned by ``hpc_agent.models.mapreduce.reduce.classify.classify_failure``.
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
# (``ops.recover.failure_signatures.CATALOG`` and
# ``ops.recover.runner_failures``'s ``_FAILURE_CATEGORY_PATTERNS``) —
# five emissions (``import_error``, ``file_not_found``,
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
]

# ── runtime ──────────────────────────────────────────────────────────────────

# Optional execution runtime override. Today only ``uv`` is supported.
Runtime = Literal["uv"]

# Submit backend names exposed on the wire — the curated scheduler
# families. All resolve to the remote-over-ssh variant since submit-flow
# only ever submits across an SSH boundary. ``pbspro`` and ``torque`` are
# the two PBS forks (distinct command grammars; see SchedulerProfile).
BackendName = Literal["sge", "slurm", "pbspro", "torque"]
