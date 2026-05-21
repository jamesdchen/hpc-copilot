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
# 'slurm' covers Slurm-Workload-Manager clusters.
Scheduler = Literal["sge", "slurm"]

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
    "internal",
]

# ── failure categories ───────────────────────────────────────────────────────

# Values returned by ``hpc_agent.mapreduce.reduce.classify.classify_failure``.
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
# Superset of ``FailureCategory`` plus ``"preempted"`` — the classifier
# never emits "preempted" directly (it's a scheduler-level state, not a
# stderr-fingerprint match), but the agent may call resubmit with
# category="preempted" when the cluster bumped a campus user.
FailureCategoryResubmittable = Literal[
    "gpu_oom",
    "system_oom",
    "segv",
    "walltime",
    "node_failure",
    "queue_stall",
    "code_bug",
    "unknown",
    "preempted",
]

# ── runtime ──────────────────────────────────────────────────────────────────

# Optional execution runtime override. Today only ``uv`` is supported.
Runtime = Literal["uv"]

# Submit backend names exposed on the wire. The registered backend keys
# in ``infra.backends`` are ``sge`` and ``slurm``; both resolve to the
# remote-over-ssh variant since submit-flow only ever submits across an
# SSH boundary (the local SGE/Slurm backend classes are kept as base
# classes for the remote subclasses but are not registered).
BackendName = Literal["sge", "slurm"]
