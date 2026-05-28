"""Pydantic model for ``<experiment>/.hpc/axes.yaml``.

Per-experiment hints for two orthogonal concerns:

* **Scheduling** (``axes`` / ``homogeneous_axes``) — which sweep
  dimension is promoted onto the SGE/SLURM task array, picked by
  runtime homogeneity. Schema v1.
* **Correctness** (``executors``) — how each ``@register_run`` function's
  totally-ordered series may be split: the classified
  :data:`~hpc_agent.experiment_kit.axis.DataAxis`. Schema v2 (additive).

The framework only stores fields it can independently act on;
experiment-specific reasoning about WHY an axis is homogeneous (or WHY a
series is a bounded halo) lives in the agent's chat context, not here.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

# A non-empty axis-name string. Mirrors the per-item ``minLength: 1``
# the hand-authored schema enforced.
_AxisName = Annotated[str, StringConstraints(min_length=1)]


class _AxisEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    size: int = Field(ge=1)


class _HaloConfig(BaseModel):
    """The bounded look-back distance of a :class:`BoundedHalo` series axis."""

    model_config = ConfigDict(extra="forbid")

    expr: str = Field(
        min_length=1,
        description=(
            "Arithmetic expression giving the warm-up row count, over the "
            "run()'s own parameter names (bare names, resolved from the "
            "sweep point), e.g. 'train_window * 48'. Evaluated by "
            "hpc_agent.experiment_kit.axis_config with a restricted AST walk — "
            "only names, numeric literals, + - * //, and min()/max() are "
            "permitted; never eval()."
        ),
    )


class _DataAxisConfig(BaseModel):
    """A classified :data:`~hpc_agent.experiment_kit.axis.DataAxis`, serialized.

    The (de)serializer is :mod:`hpc_agent.experiment_kit.axis_config`.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["independent", "associative", "bounded_halo", "sequential", "cartesian"] = Field(
        description=(
            "How the series axis is safe to split. 'independent': the loop "
            "body is a pure function of its row. 'associative': it "
            "accumulates an associative summary — also set `monoid`. "
            "'bounded_halo': it depends on a bounded look-back window — "
            "also set `halo`. 'sequential': unbounded / order-dependent "
            "state, not splittable; the fail-safe default. 'cartesian': "
            "there is no ordered series to split — a plain cartesian sweep "
            "(distinct from 'independent', which has a parallelizable series). "
            "Recorded only on a confident no-loop signal; anything ambiguous "
            "stays 'sequential'."
        ),
    )
    halo: _HaloConfig | None = Field(
        default=None,
        description="Required for kind='bounded_halo'; forbidden otherwise.",
    )
    monoid: Literal["sum", "moments"] | None = Field(
        default=None,
        description="For kind='associative': the built-in monoid chunks reduce with.",
    )

    @model_validator(mode="after")
    def _check_kind_fields(self) -> _DataAxisConfig:
        if self.kind == "bounded_halo" and self.halo is None:
            raise ValueError("data_axis kind 'bounded_halo' requires a 'halo' block")
        if self.kind != "bounded_halo" and self.halo is not None:
            raise ValueError(f"data_axis kind {self.kind!r} must not carry a 'halo' block")
        if self.kind != "associative" and self.monoid is not None:
            raise ValueError(f"data_axis kind {self.kind!r} must not carry a 'monoid'")
        # An 'associative' axis without an explicit monoid defaults to
        # 'moments' — store it on self so the recorded block is unambiguous
        # and the downstream consumer in classify_axis sees the same value.
        if self.kind == "associative" and self.monoid is None:
            self.monoid = "moments"
        return self


class _ExecutorEntry(BaseModel):
    """One ``@register_run`` function's classified series axis + provenance."""

    model_config = ConfigDict(extra="forbid")

    run_signature_sha: str = Field(
        min_length=1,
        description=(
            "Stable hash of the run()'s AST-extracted signature (the "
            "Flag list discover_runs synthesizes). A mismatch against the "
            "live run means the signature changed → re-classify."
        ),
    )
    data_axis: _DataAxisConfig
    classified_by: Literal["interview", "recall", "manual", "agent"] = Field(
        description="How the classification was reached.",
    )
    classified_at: str = Field(
        min_length=1,
        description="ISO-8601 UTC timestamp the classification was recorded.",
    )


class AxesConfig(BaseModel):
    """Schema for ``<experiment>/.hpc/axes.yaml``."""

    model_config = ConfigDict(extra="forbid", title="experiment axes config")

    # v1 files carry version 1 and validate unchanged under this model;
    # every write the framework makes now stamps version 2.
    axes_schema_version: Literal[1, 2]
    axes: list[_AxisEntry] | None = Field(
        default=None,
        description=(
            "Ordered list of every parallel axis in the experiment. "
            "Order is significant: it defines the cartesian-product "
            "convention by which task_id maps to axis values (last "
            "axis varies fastest, numpy/row-major). Required for "
            "submit-flow's wave_map building; the homogeneous_axes "
            "hint can stand alone for the cold-start picker without it."
        ),
    )
    homogeneous_axes: list[_AxisName] | None = Field(
        default=None,
        description=(
            "Names of axes the deployer believes have low runtime-cost "
            "variance. The cold-start axis_picker promotes the first "
            "one onto the task array. Once runtime priors exist, the "
            "warm-path picker uses observed CV instead. Must be a "
            "subset of axes when axes is present."
        ),
        # Pydantic's list[str] does not natively emit uniqueItems;
        # inject it via json_schema_extra so the wire constraint
        # (preserved from the hand-authored schema) survives. The
        # field_validator below enforces the same constraint at
        # validation time so Pydantic and the emitted schema agree.
        json_schema_extra={"uniqueItems": True},
    )
    executors: dict[str, _ExecutorEntry] | None = Field(
        default=None,
        description=(
            "Per-@register_run-function classified DataAxis (schema v2, "
            "additive). Keyed by the run function's name. Records how the "
            "experiment's totally-ordered series may be split correctly — "
            "orthogonal to the homogeneous_axes scheduling hint above. "
            "Written by the `classify-axis` primitive at interview time."
        ),
    )

    @field_validator("homogeneous_axes")
    @classmethod
    def _check_homogeneous_axes_unique(cls, v: list[str] | None) -> list[str] | None:
        if v is not None and len(set(v)) != len(v):
            raise ValueError("homogeneous_axes must contain unique values")
        return v

    @model_validator(mode="after")
    def _check_homogeneous_axes_subset(self) -> AxesConfig:
        if self.axes is not None and self.homogeneous_axes:
            known = {a.name for a in self.axes}
            extra = [n for n in self.homogeneous_axes if n not in known]
            if extra:
                raise ValueError(
                    f"homogeneous_axes {extra!r} not present in axes (known: {sorted(known)})"
                )
        return self
