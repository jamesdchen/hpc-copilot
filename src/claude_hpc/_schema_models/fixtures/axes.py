"""Pydantic model for ``<experiment>/.hpc/axes.yaml``.

Per-experiment hints for the axis_picker. The framework only stores
fields it can independently act on; experiment-specific reasoning
about WHY an axis is homogeneous lives in the agent's chat context,
not here.
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


class AxesConfig(BaseModel):
    """Schema for ``<experiment>/.hpc/axes.yaml``."""

    model_config = ConfigDict(extra="forbid", title="experiment axes config")

    axes_schema_version: Literal[1]
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
