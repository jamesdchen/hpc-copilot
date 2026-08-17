"""Pydantic models for the ``adopt-run`` primitive's wire contract.

``adopt-run`` is the first-class ingest path for a run submitted OUTSIDE
hpc-agent (a freestyle ``sbatch``/``qsub``): it writes the per-run sidecar
through the existing ``write-run-sidecar`` primitive (its guards + identity
cross-check apply unchanged), mints the journal record through the existing
record path WITHOUT any scheduler call, and — for an already-terminal
adoption — settles the run through the settle-run mechanism on directed
evidence. It composes existing state-layer functions; it never invents a
parallel bookkeeping mechanism.

Identity doctrine: ``cmd_sha`` is ALWAYS derived in code as
``sha256(command.strip())`` (full 64 hex) — never caller-supplied
(``extra="forbid"`` refuses a smuggled ``cmd_sha`` field at the wire).

I/O contracts:

* Input: ``schemas/adopt_run.input.json`` (from ``AdoptRunInput``).
* Output: ``schemas/adopt_run.output.json`` (from ``AdoptRunResult``).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import RunIdStrict, SchedulerJobId, SshTarget


class AdoptRunInput(BaseModel):
    """Inputs to ``adopt-run``: the foreign run's identity + cluster contract.

    ``job_ids`` present ⇒ an IN-FLIGHT adoption (the run is still live on the
    scheduler; the record lands ``in_flight`` and hands off to
    ``status-watch``). ``job_ids`` absent ⇒ an ALREADY-TERMINAL adoption,
    which then REQUIRES ``terminal_evidence`` (the settle-run doctrine: a
    settle with no evidence is a bare status flip and is refused).
    """

    model_config = ConfigDict(extra="forbid", title="adopt-run input spec")

    run_id: RunIdStrict = Field(
        description=(
            "The run_id to adopt the foreign run under. Must not already have "
            "a sidecar or journal record — adopt-run never clobbers."
        ),
    )
    command: str = Field(
        min_length=1,
        description=(
            "The command the foreign run actually executed. cmd_sha is ALWAYS "
            "derived in code as sha256 of the stripped command (full 64 hex) — "
            "never caller-supplied."
        ),
    )
    cluster: str = Field(min_length=1, description="Cluster the foreign run ran on.")
    ssh_target: SshTarget = Field(
        description="user@host the run's cluster is reached at (recorded on the journal record).",
    )
    remote_path: str = Field(
        min_length=1,
        description="Remote experiment root the foreign run's outputs live under.",
    )
    profile: str | None = Field(
        default=None,
        description="Optional profile label for the journal record; defaults to 'adopted'.",
    )
    # SchedulerJobId (digit-leading) refuses fabricated placeholder ids at the
    # journal boundary — recording prose like "purged-completed" poisons every
    # downstream alive-check/qacct probe (same doctrine as submit).
    job_ids: list[SchedulerJobId] | None = Field(
        default=None,
        min_length=1,
        description=(
            "Scheduler job ids of the still-live foreign submission. Present ⇒ "
            "in-flight adoption (record lands in_flight, next_block=status-watch). "
            "Absent ⇒ already-terminal adoption (terminal_evidence required)."
        ),
    )
    executor: str | None = Field(
        default=None,
        description=(
            "The REAL per-task command, if it differs from `command`. Defaults "
            "to `command`. Must satisfy write-run-sidecar's real-per-task-command "
            "guard (no dispatcher, no bare script, no {placeholder} leakage)."
        ),
    )
    result_dir_template: str | None = Field(
        default=None,
        description=(
            "Per-task result directory template (needs a per-task placeholder "
            "such as {task_id} when task_count > 1). When null, adopt-run infers "
            "it from results_sample; inference impossible ⇒ an elicitation "
            "envelope, never a guess."
        ),
    )
    task_count: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Number of tasks the foreign run comprised. When null, inferred from "
            "results_sample (max trailing task index + 1, gap-safe)."
        ),
    )
    summary_artifact: str | None = Field(
        default="metrics.json",
        description=(
            "Per-task summary filename the reducer reads; default metrics.json. "
            "Layout inference verifies its presence in the sampled task dirs."
        ),
    )
    results_sample: str | None = Field(
        default=None,
        description=(
            "LOCAL path/glob anchor onto the run's result tree (a task-dir "
            "parent, one task dir, or a glob over task dirs) used to INFER "
            "result_dir_template + task_count when they are missing: glob task "
            "dirs, detect the trailing-integer task pattern, verify "
            "summary_artifact presence. adopt-run declares file_write only and "
            "never probes over ssh — a remote / non-resolving anchor yields an "
            "elicitation envelope instead of a guess."
        ),
    )
    terminal_evidence: str | None = Field(
        default=None,
        description=(
            "REQUIRED when job_ids is absent: WHAT proves the foreign run's "
            "terminal state (e.g. 'reporter RC=0 all-100; result tree on disk'). "
            "Journaled as a directed-settle sign-off (scope run, response y, "
            "block adopt-run) — an empty value is refused, same doctrine as "
            "settle-run."
        ),
    )
    job_name: str | None = Field(
        default=None,
        description="Scheduler job name; defaults to run_id on the journal record.",
    )
    resources: dict[str, Any] | None = Field(
        default=None,
        description="Optional resource asks the foreign run used (recorded verbatim on the sidecar).",
    )


class AdoptRunResult(BaseModel):
    """The adoption outcome — what was recorded and where to go next.

    ``stage_reached``:

    * ``adopted_in_flight`` — sidecar + journal record written (status
      ``in_flight``); ``next_block`` hands off to ``status-watch``.
    * ``adopted_terminal`` — sidecar + journal record written and the run
      settled ``complete`` through the settle-run mechanism on the directed
      ``terminal_evidence``; ``next_block`` hands off to ``aggregate-check``.
    * ``needs_elicitation`` — the result layout could not be inferred; NOTHING
      was written. ``needs_decision`` is True and the reason names exactly what
      to supply (``result_dir_template`` + ``task_count``). ``next_block`` is
      null (a human branch).
    """

    model_config = ConfigDict(extra="forbid", title="adopt-run output data")

    stage_reached: Literal[
        "adopted_in_flight",
        "adopted_terminal",
        "needs_elicitation",
    ] = Field(description="The boundary the adoption stopped at.")
    needs_decision: bool = Field(
        description="True only on needs_elicitation — the caller must supply the named layout fields.",
    )
    reason: str = Field(
        default="",
        description="Human-readable one-line summary of the adoption outcome.",
    )
    run_id: str = Field(description="The adopted run's run_id.")
    cmd_sha: str = Field(
        description="The DERIVED parameter identity: sha256 of the stripped command (64 hex).",
    )
    status: str | None = Field(
        default=None,
        description="Journal status after adoption: in_flight, complete, or null on needs_elicitation.",
    )
    job_ids: list[str] = Field(
        default_factory=list,
        description="Job ids recorded on the journal record ([] on the terminal branch).",
    )
    task_count: int | None = Field(
        default=None,
        description="The recorded (caller-supplied or inferred) task count.",
    )
    result_dir_template: str | None = Field(
        default=None,
        description="The recorded (caller-supplied or inferred) result_dir_template.",
    )
    sidecar_path: str | None = Field(
        default=None,
        description="Absolute path of the written sidecar (null on needs_elicitation).",
    )
    next_block: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The {verb, why, spec_hint} hand-off — status-watch on the in-flight "
            "branch, aggregate-check on the terminal branch, null on "
            "needs_elicitation. verify-reproduction in external-baseline mode is "
            "the claim-comparison path once results are aggregated."
        ),
    )
