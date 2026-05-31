"""Wire models for the ``validate-campaign`` workflow.

The workflow composes the three atomic validators (executor signatures,
input dataset, walltime vs. history) plus the existing primitives
(interview, validate, preflight, runtime_prior). It returns a
:class:`ValidateCampaignReport` whose ``findings`` field is the
canonical agent-actionable contract: every atom emits
:class:`ValidatorFinding` instances with a machine-readable ``code``
and a ``suggested_fix`` hint, and the workflow concatenates them.

The agent loop reads the report, branches on
``finding.severity``+``finding.code``, applies the suggested fix
when possible, then re-runs validation. There is no ``--force``
escape hatch — if a rule is wrong for a project, the response is
to edit ``.hpc/playbook.yaml`` (per-rule, version-controlled) rather
than override the whole layer at runtime.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ValidatorFinding(BaseModel):
    """One actionable finding emitted by a single atomic validator.

    Shared across every validator so the agent loop can branch on
    ``code`` uniformly. Fields:

    * ``validator`` — name of the validator that emitted the finding
      (e.g. ``"validate-executor-signatures"``); the workflow report
      collects findings from every atom and prefixes by validator so
      the agent knows where each came from.
    * ``severity`` — ``"error"`` blocks submission, ``"warning"`` is
      informational, ``"info"`` is purely advisory.
    * ``code`` — machine-readable enum the agent branches on (e.g.
      ``"missing_parameter"``, ``"row_index_oob"``,
      ``"walltime_below_p95"``).
    * ``message`` — human-readable summary surfaced in the report.
    * ``suggested_fix`` — optional hint for the agent ("increase
      walltime to >= 7200s" / "use --gpu a100"). Lets the agent loop
      apply a fix without LLM-reasoning the recovery path.
    * ``evidence`` — validator-specific raw values (the ``p95_sec``,
      the ``requested_walltime_sec``, the missing ``param_name``).
    * ``file`` / ``line`` — when the finding is sourced from a code
      location (executor module, tasks.py), surface it for line-level
      evidence.
    """

    model_config = ConfigDict(extra="forbid")

    validator: str = Field(min_length=1)
    severity: Literal["error", "warning", "info"]
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    suggested_fix: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    file: str | None = Field(default=None, min_length=1)
    line: int | None = Field(default=None, ge=1)


class ValidateCampaignSpec(BaseModel):
    """Input spec for the ``validate-campaign`` workflow primitive.

    Most fields are optional — every atomic validator is independently
    skippable. A workflow with all atoms disabled returns a passing
    report with no findings (still useful as a smoke test of the
    composer wiring).
    """

    model_config = ConfigDict(extra="forbid")

    profile: str = Field(min_length=1)
    cluster: str = Field(min_length=1)

    # Atomic-validator inputs. Each is optional; the workflow skips
    # any validator whose required spec is None.
    executor_module: str | None = Field(
        default=None,
        description=(
            "Dotted Python import path of the user's executor module "
            "(e.g. 'src.train'). When set, validate-executor-signatures "
            "runs against ``tasks.py.resolve(0)``'s kwargs."
        ),
    )
    executor_function: str | None = Field(
        default=None,
        description="Function name in ``executor_module`` whose signature is checked.",
    )
    dataset_path: str | None = Field(
        default=None,
        description="Path to the input dataset (parquet / csv / jsonl).",
    )
    dataset_loader: Literal["parquet", "csv", "jsonl"] | None = None
    dataset_row_indices: list[int] | None = Field(
        default=None,
        description=(
            "Indices that ``tasks.py`` references; validated against actual length + non-null cols."
        ),
    )
    dataset_required_non_null_cols: list[str] = Field(default_factory=list)

    requested_walltime_sec: int | None = Field(default=None, ge=1)
    gpu_type: str | None = None
    workload_tags: list[str] = Field(
        default_factory=list,
        description=(
            "Project-specific tags (e.g. 'attn-fp32', 'mixed-precision') "
            "looked up against ``.hpc/playbook.yaml`` known-bad combos. "
            "Empty list disables the playbook lookup."
        ),
    )

    # Closed-loop campaign integration — when both fields are set, the
    # workflow invokes validate-stochastic-marker to catch the silent-
    # dedup bug class (stochastic strategies re-picking the same params
    # across iterations, making cmd_sha collide and submit-flow dedupe).
    campaign_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9._\-]+$",
        description=(
            "Closed-loop campaign slug. When set together with "
            "``expected_cmd_sha``, the workflow invokes "
            "validate-stochastic-marker to detect cmd_sha collisions "
            "against prior iterations of this campaign — catches the "
            "Optuna / random-search / PBT silent-dedup bug class."
        ),
    )
    expected_cmd_sha: str | None = Field(
        default=None,
        # Match the inner ``ValidateStochasticMarkerSpec.expected_cmd_sha``
        # min_length=8 + hex pattern; the workflow used to accept any
        # non-empty string and then crash inside the inner construction
        # with a Pydantic ValidationError rather than a structured
        # spec_invalid envelope.
        min_length=8,
        pattern=r"^[0-9a-f]{8,64}$",
        description=(
            "The cmd_sha the about-to-submit run will have. Required "
            "alongside ``campaign_id`` to enable the stochastic-marker "
            "check; ignored otherwise. Minimum 8 hex chars (matches the "
            "inner stochastic-marker validator's constraint)."
        ),
    )

    # Local pre-flight execution gate (#205). When ``result_dir_template``
    # is set, the workflow invokes ``dry-run-local`` — the only gate that
    # exercises the EXECUTION path locally before any SSH. The template
    # render is default-on (it catches the broken-grid class — an unfilled
    # placeholder or a cross-id collision — that otherwise only surfaces at
    # the cluster canary). The executor smoke-exec is OPT-IN via
    # ``dry_run_smoke`` because a local run can't stand in for the cluster
    # (modules / GPUs / scale); it complements the canary, never replaces it.
    result_dir_template: str | None = Field(
        default=None,
        description=(
            "The per-run result-directory template. When set, the workflow "
            "runs dry-run-local: it renders the template for the sampled "
            "tasks.resolve(i) ids and flags unfilled {fields} / cross-id "
            "result_dir collisions before any SSH. ``None`` skips the gate."
        ),
    )
    dry_run_smoke: bool = Field(
        default=False,
        description=(
            "Opt in to dry-run-local's executor smoke-exec: actually run the "
            "executor locally for ONE sampled grid point (import/--help-level "
            "by default) to catch import + arg-binding bugs before the cluster. "
            "Requires ``executor``. Off by default — the template render still runs."
        ),
    )
    executor: str | None = Field(
        default=None,
        description=(
            "The real per-task command (e.g. `python train.py --seed $SEED`), "
            "passed to dry-run-local's smoke-exec. Only consulted when "
            "``dry_run_smoke`` is set."
        ),
    )
    smoke_command: str | None = Field(
        default=None,
        description=(
            "Override dry-run-local's smoke command with a cheap import/--help "
            "probe (e.g. `python -c 'import train'`). Falls back to ``executor`` "
            "verbatim when omitted."
        ),
    )


class ValidateCampaignReport(BaseModel):
    """Workflow output: aggregated findings + per-validator raw output.

    ``overall`` is derived from the most-severe finding:

    * any ``error`` → ``"fail"``
    * else any ``warning`` → ``"warn"``
    * else → ``"pass"``

    The submit-flow hook aborts when ``overall == "fail"``. Warnings
    don't block; the agent prints them but proceeds.
    """

    model_config = ConfigDict(extra="forbid")

    overall: Literal["pass", "warn", "fail"]
    findings: list[ValidatorFinding] = Field(default_factory=list)
    validators_run: list[str] = Field(
        default_factory=list,
        description="Names of atomic validators that actually ran (skipped ones omitted).",
    )
