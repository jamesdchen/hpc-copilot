"""Pydantic model for the ``build-tasks-py`` scaffold's input."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _AxisSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    values: list[Any] = Field(min_length=1)


class _FlagSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    # Restricted to the four whitelist entries in
    # ``hpc_agent.atoms.build_tasks_py._FLAG_TYPE_NAMES``. Before v3 this
    # was an unconstrained ``str`` whose value was rendered verbatim into
    # ``.hpc/tasks.py``'s ``flag(name, <type_token>)`` call — any non-token
    # string (``"__import__('os').system('rm -rf /')"`` etc) detonated on
    # the next ``import tasks`` (v3 BUG-3V3-1, code injection at spec
    # boundary). New ctors must be added to ``_FLAG_TYPE_NAMES`` first.
    type: Literal["int", "float", "str", "bool"] = Field(
        description="One of int|float|str|bool — the four ctors the scaffold knows how to emit.",
    )
    default: Any | None = None


class _DataAxisSpec(BaseModel):
    """The classified series axis — drives planner-mode codegen.

    When present on :class:`BuildTasksPyInput`, ``build-tasks-py`` emits a
    ``hpc_agent.template.plan_tasks``-based ``tasks.py`` (the deterministic
    materialisation of the ``/submit-hpc`` Step 3 ``DataAxis`` inference)
    instead of a cartesian-product one. The cartesian ``axes`` become the
    *sweep* and the series axis is partitioned by the planner.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["independent", "associative", "bounded_halo", "sequential"] = Field(
        description="DataAxis classification of the series axis — see hpc_agent.template.axis.",
    )
    chunks: int = Field(
        default=1, ge=1, description="Chunks per sweep point. Ignored for kind='sequential'."
    )
    series_length: int = Field(
        ge=0, description="Length of the series being partitioned (probed by the agent at Step 3)."
    )
    halo_expr: str | None = Field(
        default=None,
        description=(
            "Required for kind='bounded_halo': a Python ARITHMETIC expression over the "
            "`params` dict giving the warm-up row count, e.g. \"params['train_window'] * 48\". "
            "Validated to arithmetic-only (no calls/attributes) before it is rendered."
        ),
    )
    monoid: Literal["sum", "moments"] | None = Field(
        default=None,
        description="For kind='associative': which built-in monoid chunks reduce with (default: moments).",
    )


class BuildTasksPyInput(BaseModel):
    """Axes spec + per-executor flag declarations for the ``tasks.py`` scaffold.

    Drives ``hpc_agent.atoms.build_tasks_py`` to scaffold
    ``<experiment>/.hpc/tasks.py``. Two modes:

    * **cartesian** (``data_axis`` omitted) — ``axes`` is a cartesian
      product, one independent task per cell.
    * **planner** (``data_axis`` present) — ``axes`` is the *sweep* and
      the series axis is partitioned by ``hpc_agent.template.plan_tasks``
      per the classified ``DataAxis``.
    """

    model_config = ConfigDict(extra="forbid", title="build-tasks-py input")

    axes: list[_AxisSpec] = Field(min_length=1)
    flags_by_executor: dict[str, list[_FlagSpec]] = Field(min_length=1)
    force: bool | None = None
    data_axis: _DataAxisSpec | None = Field(
        default=None,
        description="When set, emit a planner-driven tasks.py instead of a cartesian one.",
    )
