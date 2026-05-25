"""Pydantic model for the ``build-tasks-py`` scaffold's input."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _AxisSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    values: list[Any] = Field(min_length=1)


class _FlagSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    # Restricted to the four whitelist entries in
    # ``hpc_agent.incorporation.build.tasks_py._FLAG_TYPE_NAMES``. Before v3 this
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
    ``hpc_agent.experiment_kit.plan_tasks``-based ``tasks.py`` (the deterministic
    materialisation of the ``/submit-hpc`` Step 3 ``DataAxis`` inference)
    instead of a cartesian-product one. The cartesian ``axes`` become the
    *sweep* and the series axis is partitioned by the planner.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["independent", "associative", "bounded_halo", "sequential"] = Field(
        description=(
            "How the series axis is safe to split (classify by reading the experiment's "
            "loop and its call graph). 'independent': the loop body is a pure function of "
            "its row, no accumulator. 'associative': it accumulates an associative summary "
            "(sum / count / min-max / sufficient statistics) — also set `monoid`. "
            "'bounded_halo': it refits or re-reads a trailing window of bounded length "
            "(a rolling statistic, a `train_window` look-back) — also set `halo_expr`. "
            "'sequential': unbounded or order-dependent state — not splittable, and the "
            "fail-safe default whenever the dependency structure is not unambiguous. The "
            "classification must be verified with the serial-elision gate "
            "(hpc_agent.experiment_kit.check_elision) before submitting — a misclassified axis "
            "returns plausible-but-wrong numbers."
        ),
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

    @model_validator(mode="after")
    def _check_kind_fields(self) -> _DataAxisSpec:
        # Mirror the invariant enforced by _DataAxisConfig in
        # fixtures/axes.py so the kind-conditional fields are validated
        # at the schema boundary instead of leaking through to
        # incorporation/build/tasks_py.py as a SpecInvalid.
        if self.kind == "bounded_halo" and self.halo_expr is None:
            raise ValueError("data_axis kind 'bounded_halo' requires 'halo_expr'")
        if self.kind != "bounded_halo" and self.halo_expr is not None:
            raise ValueError(f"data_axis kind {self.kind!r} must not carry 'halo_expr'")
        if self.kind != "associative" and self.monoid is not None:
            raise ValueError(f"data_axis kind {self.kind!r} must not carry 'monoid'")
        return self


class BuildTasksPyInput(BaseModel):
    """Axes spec + per-executor flag declarations for the ``tasks.py`` scaffold.

    Drives ``hpc_agent.incorporation.build.tasks_py`` to scaffold
    ``<experiment>/.hpc/tasks.py``. Two modes:

    * **cartesian** (``data_axis`` omitted) — ``axes`` is a cartesian
      product, one independent task per cell.
    * **planner** (``data_axis`` present) — ``axes`` is the *sweep* and
      the series axis is partitioned by ``hpc_agent.experiment_kit.plan_tasks``
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
