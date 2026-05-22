"""Pydantic model for the ``classify-axis`` scaffold's input.

``classify-axis`` records one ``@register_run`` function's classified
:data:`~hpc_agent.template.axis.DataAxis` into
``<experiment>/.hpc/axes.yaml``'s ``executors`` block. The agent does
the *classification* (reading ``run()``, conducting the interview); this
spec is the resolved answer the primitive persists.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Reuse the exact persisted-shape model so the wire validation of the
# data_axis block here is byte-identical to what axes.yaml enforces.
from hpc_agent._schema_models.fixtures.axes import _DataAxisConfig


class ClassifyAxisInput(BaseModel):
    """The resolved classification for one ``@register_run`` function."""

    model_config = ConfigDict(extra="forbid", title="classify-axis input")

    run_name: str = Field(
        min_length=1,
        description=(
            "The @register_run function's name — the key under which the "
            "classification is stored in axes.yaml's `executors` block."
        ),
    )
    run_signature_sha: str = Field(
        min_length=1,
        description=(
            "The run's current signature hash (RunInfo.run_signature_sha "
            "from discover_runs). Stored so a later submit can detect a "
            "signature change and re-interview instead of reusing a stale "
            "classification."
        ),
    )
    data_axis: _DataAxisConfig = Field(
        description=(
            "The classified series axis: {kind, halo?, monoid?}. The "
            "classification must clear the serial-elision gate "
            "(hpc_agent.template.check_elision) before any cluster time is "
            "spent — a misclassified axis returns plausible-but-wrong "
            "numbers."
        ),
    )
    classified_by: Literal["interview", "recall", "manual"] = Field(
        default="interview",
        description=(
            "How the classification was reached: 'interview' (the agent "
            "ran the classification interview), 'recall' (a prior similar "
            "experiment's classification was reused), or 'manual' (the "
            "operator stated it directly)."
        ),
    )
