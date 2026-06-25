"""Pydantic models for the ``apply-safe-defaults`` query.

``apply-safe-defaults`` is the autonomous-caller counterpart to a human
walking the ``needs_resolution`` dialog: it consumes the ``{resolved,
ambiguities}`` envelope ``walk-submit-ambiguities`` produced and fills
each ambiguity from its own ``safe_default``. Because the field partition
never attaches a ``safe_default`` to a REQUIRED_CALLER_FIELDS member, this
verb structurally CANNOT fill ``task_generator`` — and it re-checks
:func:`hpc_agent.ops.submit.field_partition.may_have_safe_default` as
defense-in-depth, raising ``spec_invalid`` if asked.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ApplySafeDefaultsInput(BaseModel):
    """The ``{resolved, ambiguities}`` envelope to auto-fill.

    Shaped exactly like ``walk-submit-ambiguities``'s output so the two
    chain without reshaping: the autonomous caller pipes the walk's
    ``data`` straight into this verb's ``--spec``.
    """

    model_config = ConfigDict(extra="forbid", title="apply-safe-defaults input")

    resolved: dict[str, Any] = Field(
        default_factory=dict,
        description="Fields the walk already resolved; auto-filled defaults merge on top.",
    )
    ambiguities: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Ambiguity dicts from walk-submit-ambiguities. Each is filled from its "
            "safe_default when present; a required-caller field (no safe_default) "
            "stays unresolved and is reported."
        ),
    )


class ApplySafeDefaultsResult(BaseModel):
    """The result of auto-filling: merged ``resolved`` plus what stayed open."""

    model_config = ConfigDict(extra="forbid", title="apply-safe-defaults output")

    resolved: dict[str, Any] = Field(
        description="The input `resolved` merged with every applied safe_default.",
    )
    applied: dict[str, Any] = Field(
        description="field → value for each ambiguity auto-filled from its safe_default.",
    )
    still_unresolved: list[str] = Field(
        description=(
            "Fields that could NOT be auto-filled — a REQUIRED_CALLER_FIELDS member "
            "(no safe_default by partition), or an ambiguity whose safe_default was "
            "absent. These remain for the caller; chiefly goal / task_generator."
        ),
    )
    all_resolved: bool = Field(
        description="True iff still_unresolved is empty (the spec is ready to hand off).",
    )
