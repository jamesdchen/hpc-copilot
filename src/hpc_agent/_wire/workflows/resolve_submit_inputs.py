"""Pydantic models for the ``resolve-submit-inputs`` workflow primitive.

The deterministic submit *input-resolution* chain as ONE call — the
control-flow-out-of-the-LLM move applied to ``worker_prompts/submit.md``
Steps 6a-6d (the run_id + resume-detection + tasks.py-scaffold +
submit-spec-assembly spine that runs AFTER intent is parsed and the
data-axis is resolved). Those steps are mechanical: compute the run_id,
look up a prior run and branch on it, scaffold (or reuse) ``.hpc/tasks.py``,
then assemble the validated submit-flow spec. ``resolve-submit-inputs``
runs that branch logic in code and reports a single typed ``stage_reached``
outcome, so the agent stops hand-walking (and hand-branching) the four verbs.

Composition (all on the laptop, no cluster / SSH):

    compute-run-id  →  find-prior-run  →  (build-tasks-py if tasks.py absent)
                    →  build-submit-spec  →  write-run-sidecar

The ``resolved`` terminal is fully submit-ready — the submit-flow spec is built
AND the per-run sidecar is written (the #171 write-first precondition) — so the
caller hands ``submit_spec`` straight to ``submit-pipeline``.

The genuine JUDGEMENT that precedes this spine stays UPSTREAM as
escalations — parsing the user's natural-language intent (Step 2),
classifying the data-axis when unresolved (Step 3), and env selection
(Step 4). This composite is what runs once those are resolved; the
caller hands it the already-resolved values (``run_name`` + the
``build-submit-spec`` inputs + an optional pre-classified
``build-tasks-py`` spec) and reads ``stage_reached``.

**Additive.** Does not replace the per-verb worker-prompt path — it is a
new verb the prompt may adopt. Nothing breaks if it is not yet wired in,
which is why it ships before the prompt is restructured.

I/O contracts:

* Input: ``schemas/resolve_submit_inputs.input.json`` (from
  ``ResolveSubmitInputsSpec``).
* Output: ``schemas/resolve_submit_inputs.output.json`` (from
  ``ResolveSubmitInputsResult``).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire.actions.build_submit_spec import BuildSubmitSpecInput
from hpc_agent._wire.actions.build_tasks_py import BuildTasksPyInput
from hpc_agent._wire.actions.write_run_sidecar import WriteRunSidecarInput


class ResolveSubmitInputsSpec(BaseModel):
    """Spec passed to ``hpc-agent resolve-submit-inputs --spec <file>``.

    Carries exactly the values the deterministic chain needs that the
    caller has already resolved upstream: the human-chosen ``run_name``
    (drives ``compute-run-id``), the full ``build-submit-spec`` input
    (re-used verbatim so cluster / profile / backend / job_env / etc. are
    not re-enumerated here), and an optional ``build_tasks`` scaffold spec
    used only when ``.hpc/tasks.py`` is absent. ``experiment_dir`` is the
    framework-context positional arg, intentionally NOT a spec field.
    """

    model_config = ConfigDict(extra="forbid", title="resolve-submit-inputs input spec")

    run_name: str = Field(
        min_length=1,
        description=(
            "Human-chosen run name fed to compute-run-id; combined with the "
            "cmd_sha prefix to form run_id (<run_name>-<sha[:8]>)."
        ),
    )
    submit: BuildSubmitSpecInput = Field(
        description=(
            "The resolved build-submit-spec input (profile / cluster / "
            "ssh_target / remote_path / total_tasks / backend / job_env knobs). "
            "resolve-submit-inputs feeds this to build-submit-spec to assemble "
            "the validated submit-flow spec after the run_id / prior-run / "
            "tasks.py branches clear. Its ``run_id`` / ``cmd_sha`` are "
            "PLACEHOLDERS — the composite overrides them with the values "
            "compute-run-id derives, so the built spec always matches the "
            "reported run_id."
        ),
    )
    sidecar: WriteRunSidecarInput = Field(
        description=(
            "The resolved write-run-sidecar input (the v2 config snapshot: the "
            "REAL per-task ``executor``, result_dir_template, task_count, "
            "resources, env, constraints, runtime). resolve-submit-inputs writes "
            "the per-run sidecar from this on the ``resolved`` path, so the "
            "output is fully submit-ready (the #171 write-first precondition is "
            "already satisfied before submit-pipeline runs). Its ``run_id`` / "
            "``cmd_sha`` are PLACEHOLDERS overridden with compute-run-id's values."
        ),
    )
    build_tasks: BuildTasksPyInput | None = Field(
        default=None,
        description=(
            "Optional pre-classified build-tasks-py spec (axes + "
            "flags_by_executor + optional data_axis). Used ONLY when "
            "<experiment_dir>/.hpc/tasks.py is absent and can be scaffolded "
            "deterministically. When tasks.py is absent and this is null, the "
            "composite escalates needs_scaffold_interview rather than guessing."
        ),
    )
    reproduction_of: str | None = Field(
        default=None,
        description=(
            "Reproduction-receipt lever: the run_id of an ORIGINAL run this "
            "resolution deliberately REPRODUCES with identical params. cmd_sha "
            "is parameter identity, so a re-run of the same params otherwise "
            "stops at prior_run_found against the (even complete) original. "
            "Naming it here makes find-prior-run skip the original (and any "
            "prior reproduction of it) — so a `complete` original no longer "
            "terminates resolve, while ANY OTHER live prior still does — and "
            "stamps ``reproduces`` onto the derived run's sidecar so a later "
            "reproduction of the same original skips this one too. It is also "
            "threaded onto the built submit-flow spec so the submit-time "
            "layer-2 dedup pierces the same original. Null = ordinary submit."
        ),
    )


class ResolveSubmitInputsResult(BaseModel):
    """Shape of the ``data`` field on a ``resolve-submit-inputs`` envelope.

    ``stage_reached`` is the deterministic dispatch the agent used to walk
    by hand. ``needs_decision`` flags the outcomes that require a caller
    decision — the decision-as-data shape (#231): the composite ran every
    deterministic branch; only the genuine judgement (resume-vs-fresh on a
    live prior, or the scaffold sub-interview) is handed back.
    """

    model_config = ConfigDict(extra="forbid", title="resolve-submit-inputs output data")

    stage_reached: Literal[
        "resolved",
        "prior_run_found",
        "needs_scaffold_interview",
    ] = Field(description="Which stage the chain reached / stopped at.")
    needs_decision: bool = Field(
        description=(
            "True for prior_run_found (only the user picks resume-vs-fresh) and "
            "needs_scaffold_interview (the headless worker can't run the scaffold "
            "interview); False for the clean resolved terminal."
        ),
    )
    reason: str = Field(description="Human-readable summary of the outcome / what must be decided.")
    run_id: str | None = Field(
        default=None,
        description="The computed run_id (<run_name>-<sha[:8]>); set on every stage.",
    )
    cmd_sha: str | None = Field(
        default=None,
        description="Full 64-char cmd_sha hash of the materialized task list; set on every stage.",
    )
    submit_spec: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The built + validated submit-flow spec (a submit_flow.input.json "
            "dict), ready to hand to submit-pipeline / submit-flow. Set only on "
            "stage_reached='resolved'."
        ),
    )
    sidecar_path: str | None = Field(
        default=None,
        description=(
            "Absolute path of the per-run sidecar written on the 'resolved' path "
            "(.hpc/runs/<run_id>.json) — the #171 write-first precondition is "
            "satisfied, so submit-pipeline can run directly. Set only on "
            "stage_reached='resolved'."
        ),
    )
    prior_run_id: str | None = Field(
        default=None,
        description=(
            "Resume context — the matching live prior run's run_id on "
            "stage_reached='prior_run_found'; else null."
        ),
    )
    prior_status: str | None = Field(
        default=None,
        description=(
            "Resume context — the live prior's lifecycle status (complete / "
            "in_flight) on stage_reached='prior_run_found'; else null."
        ),
    )
    prior_cluster: str | None = Field(
        default=None,
        description=(
            "Resume context — the cluster the live prior attempt is running on, "
            "on stage_reached='prior_run_found'; else null. For a canary-only "
            "prior (an attempt that died pre-main-submit, leaving only its live "
            "<run_id>-canary sub-record + detached lease) this names the cluster "
            "the canary is in-flight against — so the human meets a retarget fork "
            "at S1 knowing the prior attempt's cluster, not just its id."
        ),
    )
