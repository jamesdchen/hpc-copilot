"""Shared Pydantic types reused across multiple wire schemas.

These mirror the named ``$defs`` inside ``schemas/envelope.json``.
Each consumer schema previously referenced those defs via cross-file
``$ref``; with Pydantic as the authoring SoT the SoT moves up one
level — a single Python alias here is imported by every model that
needs the constraint, and the emitted JSON inlines the pattern.

Tightening one alias here regenerates every consumer schema in
exactly the same way ``$ref`` resolution did before, just at build
time instead of validation time.

Aliases are deliberately ``Annotated`` rather than custom
``BaseModel`` subclasses so they inline as ``{type: ..., ...}`` in
the emitted schema without introducing a per-model ``$defs`` entry.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints

# ── identifiers ──────────────────────────────────────────────────────────────

# Strict run-identifier shape used on INPUT schemas. Filesystem-safe:
# alphanumerics, dot, underscore, hyphen. Mirrors envelope.json#/$defs/run_id_strict.
RunIdStrict = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._\-]+$")]

# Loose run-identifier used on OUTPUT schemas — any string. Output schemas
# use this so legacy sidecars validate. Mirrors envelope.json#/$defs/run_id.
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
# live in inspect-cluster; the schema only enforces non-empty.
GpuType = Annotated[str, Field(min_length=1)]

# ── error envelope ───────────────────────────────────────────────────────────

# Canonical envelope error_code enum. Output schemas that surface error
# codes inside ``data`` (e.g. failures, status, validate) must use this
# alias so the enum stays byte-equivalent to envelope.json.
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

# ── runtime ──────────────────────────────────────────────────────────────────

# Optional execution runtime override. Today only ``uv`` is supported.
Runtime = Literal["uv"]

# Submit backend names exposed on the wire. Mirrors the keys of
# ``infra.backends.BACKENDS`` minus the local-only variants — submissions
# go through SSH so the remote-* variants are the relevant ones.
BackendName = Literal["sge_remote", "slurm"]
