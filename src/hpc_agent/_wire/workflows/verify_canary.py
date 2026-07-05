"""Pydantic model for the ``verify-canary`` workflow atom's output."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

CanaryFailureKind = Literal[
    "dispatcher_failed",
    "import_error",
    "module_not_found",
    "traceback",
    "oom_killed",
    "segfault",
    # The canary task-0 ``_runtime.json`` recorded a non-zero ``exit_code`` even
    # though scheduler state + result-file presence + the 50-line stderr scan all
    # "passed" — a task that wrote a partial result then failed (#351-3). The
    # positive exit-code read in ``verify-canary`` catches it before the success
    # return and before the cmd_sha is cached as canary-validated.
    "nonzero_exit",
    "missing_output",
    # Every status poll failed — broken cluster-side reporter (the job may have
    # run but its result can't be read, so the canary can't be trusted).
    "reporter_unreachable",
    # Finding 12: consecutive DETERMINISTIC broken-env polls (rc 126/127)
    # escalated the canary early, and the env-independent ``.hpc_failed`` marker
    # scan (plain sh, survives the broken env) found the dispatcher's terminal
    # failure marker(s) — positive proof the task ran and failed even though the
    # python status reporter was unreachable.
    "canary_failed",
    # The job left the scheduler queue without recording a completion and no
    # stderr marker explains why — resolved fast instead of riding the full
    # wait budget (#193).
    "completed_unknown",
    "timeout",
    "abandoned",
    # Checkpoint-canary verdicts (#294 PR4). A run that opts into
    # auto_resume_on_kill fires a canary that writes a checkpoint then kills
    # itself; verify-canary asserts the checkpoint round-trips on the cluster.
    # "no checkpoint written" (the kill beat the write, or the executor never
    # checkpoints) and "checkpoint present but read_latest_checkpoint cannot
    # reload it" (a wrong/non-portable checkpoint format) respectively.
    "checkpoint_missing",
    "checkpoint_unloadable",
]


class CanaryFailureFeatures(BaseModel):
    """Structured diagnostic evidence attached to a failed canary envelope.

    Today (pre-fix) a canary that died cluster-side returned
    ``{failure_kind: "dispatcher_failed"}`` with no cluster context — the
    orchestrator's prose-level "go fetch the log" step was the only path
    to the actual error, and agents kept skipping it. This object moves
    that step into framework code: the same ``stderr_tail`` is restated
    under ``cluster_log_tail`` for structured consumers, and
    ``classified_error`` carries the
    :func:`hpc_agent.infra.failure_signatures.classify` result so a
    decision-maker reads an ``error_class`` + ``suggested_fix`` instead
    of paraphrasing the log.
    """

    model_config = ConfigDict(extra="forbid", title="CanaryFailureFeatures")

    cluster_log_tail: str = Field(
        description=(
            "Raw last ~50 lines of the canary's cluster log, verbatim. The "
            "same content as the top-level ``stderr_tail`` field, restated "
            "under a structured key so downstream consumers don't have to "
            "know which top-level field carries it."
        ),
    )
    log_path: str | None = Field(
        default=None,
        description=(
            "Remote path of the cluster log file the tail was read from, when "
            "known. Lets the operator ssh over and tail more if 50 lines "
            "isn't enough. ``None`` when no log was fetched."
        ),
    )
    classified_error: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The ``{error_class, suggested_fix, matched_pattern}`` triple "
            "returned by :func:`infra.failure_signatures.classify` on "
            "the cluster log tail. ``None`` when the stderr was empty "
            "(nothing to classify)."
        ),
    )


class VerifyCanaryResult(BaseModel):
    """Result of the wait + grep + output-check protocol for a 1-task canary.

    Caller branches on ``ok``: True → main array submit; False →
    surface stderr_tail to the user verbatim.
    """

    model_config = ConfigDict(extra="forbid", title="verify-canary output")

    ok: bool
    failure_kind: CanaryFailureKind | None = None
    details: str = Field(
        description="One-line human-readable summary the slash command surfaces above the raw stderr_tail.",
    )
    stderr_tail: str = Field(
        description="Last ~50 lines of the canary's stderr log (or empty string when not retrievable).",
    )
    metrics_fingerprint: str | None = Field(
        default=None,
        description=(
            "Optional sha256 of the canary's expected output file when "
            "the caller asked for a fingerprint. None on every failure "
            "path and when fingerprinting is skipped or fails."
        ),
    )
    failure_features: CanaryFailureFeatures | None = Field(
        default=None,
        description=(
            "Structured diagnostic evidence attached to failed-canary envelopes "
            "(``ok=False``). Carries the raw cluster log tail under a "
            "structured key plus a ``classify()`` result against the same "
            "failure-signature CATALOG ``ops/recover`` uses, so the "
            "orchestrator gets an actionable remediation instead of a bare "
            "``failure_kind``. ``None`` on success."
        ),
    )
