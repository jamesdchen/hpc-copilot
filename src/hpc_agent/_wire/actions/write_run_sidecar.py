"""Pydantic model for the ``write-run-sidecar`` primitive's input.

Mirrors the kwargs of :func:`hpc_agent.state.runs.write_run_sidecar`
(see `state/runs.py:184`) minus the two fields the primitive auto-stamps
(``submitted_at``, ``hpc_agent_version``) so the agent can write the
required sidecar via a single CLI invocation instead of an introspected
Python call (#200).
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hpc_agent._wire._shared import (
    CampaignId,
    RunIdStrict,
    Runtime,
    SchedulerJobId,
)

# Placeholders that are CONSTANT across all tasks in a run — using only
# these in result_dir_template renders the same dir for every task, so the
# tasks clobber each other's output. Used by the validator below.
_CONSTANT_PER_RUN_PLACEHOLDERS = frozenset({"run_id"})


def _result_dir_per_task_placeholders(template: str) -> set[str]:
    """Return ``{<name>}`` placeholder names in *template* excluding the
    constants. If the returned set is non-empty, at least one placeholder
    varies per task (either ``{task_id}`` or a swept kwarg from FLAGS) and
    each task renders to a unique directory."""
    return set(re.findall(r"\{([^}]+)\}", template)) - _CONSTANT_PER_RUN_PLACEHOLDERS


class WriteRunSidecarInput(BaseModel):
    """Resolved fields written into ``.hpc/runs/<run_id>.json``.

    All ``v2 config-snapshot`` fields are optional at the call site but
    every successful ``/submit`` should populate the ones that apply so
    downstream commands can rebuild full context without consulting any
    external config file (same convention as the underlying function's
    docstring).
    """

    model_config = ConfigDict(extra="forbid", title="write-run-sidecar input")

    # ----- required identity + cluster contract -----
    run_id: RunIdStrict
    # Same regex as build-submit-spec — accept the 8-char prefix that
    # threads through from a recall lookup, full 64-char hex too.
    cmd_sha: str = Field(pattern=r"^[0-9a-f]{8,64}$")
    # The REAL per-task command (e.g. ``python train.py --seed $SEED``).
    # NOT the job-script dispatcher command — that lives in
    # job_env["EXECUTOR"] on the submit-flow spec, not here. The
    # #162 check refuses dispatcher-shaped values on the sidecar's
    # executor field; the primitive surfaces that as a SpecInvalid.
    executor: str = Field(min_length=1)
    result_dir_template: str = Field(min_length=1)
    task_count: int = Field(ge=1)

    @model_validator(mode="after")
    def _per_task_result_dir_isolation(self) -> WriteRunSidecarInput:
        """Refuse a ``result_dir_template`` that renders to the same path for
        every task in a multi-task run.

        Empirical 2026-06-06 demo: orchestrator built a sidecar with
        ``result_dir_template = "results/{run_id}"`` and ``task_count = 100``.
        Every task ran cluster-side and wrote ``metrics.json`` into the same
        directory; the last writer won, the other 99 results clobbered. The
        framework had every input to detect this at sidecar-write time.

        Per-task uniqueness requires at least one placeholder that varies
        across tasks — either ``{task_id}`` (always varies) or a kwarg name
        from ``tasks.py`` ``FLAGS`` that's a swept axis (e.g. ``{seed}``).
        ``{run_id}`` alone is constant per run and does NOT provide isolation.
        """
        if self.task_count <= 1:
            return self
        per_task = _result_dir_per_task_placeholders(self.result_dir_template)
        if per_task:
            return self
        all_placeholders = set(re.findall(r"\{([^}]+)\}", self.result_dir_template))
        raise ValueError(
            f"result_dir_template={self.result_dir_template!r} has no per-task "
            f"placeholder, but task_count={self.task_count}. All tasks would "
            f"render to the same directory and clobber each other's output. "
            f"Found placeholders {sorted(all_placeholders) or 'none'}; only "
            f"{sorted(_CONSTANT_PER_RUN_PLACEHOLDERS & all_placeholders) or 'no'} "
            f"are constant per run. Add {{task_id}} for guaranteed uniqueness, "
            f"e.g. 'results/{{run_id}}/task_{{task_id}}', or use a swept kwarg "
            f"from tasks.py FLAGS such as 'results/{{run_id}}/seed_{{seed}}'."
        )

    # SHA of the on-disk tasks.py. Empty string disables the drift guard
    # (the dispatcher silently no-ops on '') — matches the function's
    # opt-in semantics.
    tasks_py_sha: str = Field(default="", pattern=r"^([0-9a-f]{64})?$")

    # ----- optional wave + extras -----
    wave_map: dict[str, list[int]] | None = None
    extra: dict[str, Any] | None = None

    # ----- v2 config-snapshot fields (all optional) -----
    cluster: str | None = None
    profile: str | None = None
    campaign_id: CampaignId | None = None
    project: str | None = None
    remote_path: str | None = None
    resources: dict[str, Any] | None = None
    env: dict[str, Any] | None = None
    env_group: str | None = None
    constraints: dict[str, Any] | None = None
    gpu_fallback: list[str] | None = None
    max_retries: int | None = Field(default=None, ge=0)
    runtime: Runtime | None = None
    auto_retry: dict[str, Any] | None = None
    aggregate_defaults: dict[str, Any] | None = None
    results: dict[str, Any] | None = None
    # Opaque per-task reconciliation tokens a closed-loop strategy round-trips
    # (task-ordered; e.g. an Optuna trial number per task). Recorded verbatim
    # and re-surfaced by prior_records(); never interpreted by the framework.
    trial_tokens: list[Any] | None = None
    # Run_ids whose outputs this run consumes (DAG lineage). The primitive
    # derives node_sha from these via resolve_node_sha — identity is computed
    # from the parents' on-disk sidecars, never asserted by the caller.
    parent_run_ids: list[RunIdStrict] | None = Field(default=None, min_length=1)
    # Provenance: DATA + ENVIRONMENT identity to complement cmd_sha (params)
    # and tasks_py_sha (code) — see compute_data_sha / compute_env_hash (#222).
    # Both are bare sha256 hex; an empty/absent value means "not captured".
    data_sha: str | None = Field(default=None, pattern=r"^([0-9a-f]{64})?$")
    env_hash: str | None = Field(default=None, pattern=r"^([0-9a-f]{64})?$")
    # SchedulerJobId: a sidecar's job_ids feed every alive-check/qacct probe —
    # refuse fabricated placeholders (see _shared.SchedulerJobId rationale).
    job_ids: list[SchedulerJobId] | None = None
