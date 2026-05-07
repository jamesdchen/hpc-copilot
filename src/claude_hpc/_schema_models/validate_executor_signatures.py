"""Wire model for the ``validate-executor-signatures`` atom.

Catches the SEGMENT_CHOICES bug class: a campaign's ``tasks.py``
calls ``resolve(i)`` returning kwargs that the executor function's
signature would reject (missing parameter, wrong Literal value,
wrong Enum member). Static cross-check via ``inspect.signature``
+ AST fallback when the module can't be imported safely.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from claude_hpc._schema_models.validate_campaign import (
    ValidatorFinding,  # noqa: TC001 — Pydantic resolves the annotation at runtime
)


class ValidateExecutorSignaturesSpec(BaseModel):
    """Input spec.

    *executor_module* is a dotted import path; *executor_function* is
    the public function name on that module to introspect. When the
    module cannot be imported (project-side import-time side effects,
    missing optional deps), the validator falls back to AST parsing
    and emits an ``info`` finding describing the degradation.
    """

    model_config = ConfigDict(extra="forbid")

    executor_module: str = Field(min_length=1)
    executor_function: str = Field(min_length=1)
    tasks_py_path: str = Field(
        default=".hpc/tasks.py",
        description="Path to the campaign's tasks.py (relative to experiment_dir).",
    )
    sample_n_tasks: int = Field(
        default=8,
        ge=1,
        description=(
            "Number of tasks to sample from ``tasks.py.resolve(i)`` for the "
            "signature cross-check. The first failing task surfaces a finding; "
            "sampling instead of exhaustive walk keeps the validator fast for "
            "large campaigns without losing the bug class."
        ),
    )


class ValidateExecutorSignaturesResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: list[ValidatorFinding] = Field(default_factory=list)
