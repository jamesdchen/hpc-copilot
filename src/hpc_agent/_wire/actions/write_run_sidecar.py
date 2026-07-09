"""Pydantic model for the ``write-run-sidecar`` primitive's input.

Mirrors the kwargs of :func:`hpc_agent.state.runs.write_run_sidecar`
(see `state/runs.py:184`) minus the two fields the primitive auto-stamps
(``submitted_at``, ``hpc_agent_version``) so the agent can write the
required sidecar via a single CLI invocation instead of an introspected
Python call (#200).
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from hpc_agent._wire._shared import (
    CampaignId,
    RunIdStrict,
    Runtime,
    SchedulerJobId,
)

# An opaque, caller-owned evidence-scope tag. Slug-validated with the SAME
# character class as ``RunIdStrict`` (``_shared.RunIdStrict`` is the pattern
# source; mirrored here rather than reused because a scope tag is semantically
# distinct from a run identity). Core never INTERPRETS the string — it is
# recorded verbatim on the sidecar — but a non-slug tag is refused HERE at the
# wire so a malformed tag never reaches the state layer.
ScopeTag = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._\-]+$")]

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
    # Resolved per-task params (task-ordered; one dict per task, RESERVED_TASK_KEYS
    # stripped — the cmd_sha pre-image). Persisted for provenance so a run's params
    # are recoverable from its sidecar; recorded verbatim, re-surfaced by
    # prior_records(), never interpreted. Produced by compute-run-id.
    trial_params: list[dict[str, Any]] | None = None
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
    # Opaque caller-owned evidence-scope tags. Slug-validated per element
    # (^[A-Za-z0-9._\-]+$) so a malformed tag is refused at the wire; core
    # never interprets them — they are recorded verbatim on the sidecar and
    # route as owned-by-submit-s1.
    scopes: list[ScopeTag] | None = Field(
        default=None,
        description=(
            "OPAQUE caller-owned evidence-scope tags (slug-validated: "
            "^[A-Za-z0-9._\\-]+$). Core never interprets them — they are "
            "recorded verbatim on the sidecar; a non-slug tag is refused here."
        ),
    )
    # run_id of the ORIGINAL this run deliberately reproduces (the
    # reproduction-receipt provenance field). Recorded verbatim; never
    # interpreted here. find_run_by_cmd_sha's reproduction_of lever reads it
    # back so a later reproduction of the same original skips this run too.
    reproduces: RunIdStrict | None = Field(
        default=None,
        description=(
            "run_id of the ORIGINAL run this submission deliberately REPRODUCES "
            "(the reproduction-receipt provenance field). Recorded verbatim on "
            "the sidecar; a later reproduction of the same original reads it "
            "back (find-prior-run / submit's reproduction_of lever) to skip "
            "this derived run too. Null = ordinary (non-reproduction) run."
        ),
    )
    # data-trace T3: DISCLOSURE of an exercised digest override. Recorded
    # verbatim on the sidecar when the spec-level ``trace_digests``
    # (force_on/force_off) was exercised, so a reader sees the "NO KNOB"
    # classifier was overridden. Stamped in CODE by resolve-submit-inputs; null
    # = the classifier decided unaided (the common case, omitted on write).
    trace_digests_override: Literal["force_on", "force_off"] | None = Field(
        default=None,
        description=(
            "DISCLOSURE of an exercised digest override (data-trace T3). "
            "force_on/force_off when the spec-level trace_digests lever overrode "
            "the digest classifier's sidecar-derived decision; recorded verbatim "
            "so the override is visible. Null = the classifier decided unaided "
            "(NO KNOB); omitted on write."
        ),
    )
    # OPAQUE caller-owned audit-trail identity — the sidecar echo of
    # interview.json's audited_source block (notebook-audit T14). Core never
    # interprets it; recorded verbatim on the sidecar so export-dossier can seal
    # the audit trail. Stamped in CODE by resolve-submit-inputs from
    # interview.json (not hand-authored); null = a non-audited run.
    audited_source: dict[str, Any] | None = Field(
        default=None,
        description=(
            "OPAQUE caller-owned audit-trail identity — the sidecar echo of "
            "interview.json's audited_source opt-in block ({source, template, "
            "audit_id}; notebook-audit T14). Core never interprets it; recorded "
            "verbatim so export-dossier can seal the audit trail (source .py + "
            "template .py + the notebook attestation journal). Stamped in code "
            "by resolve-submit-inputs; null = a non-audited run."
        ),
    )
