"""Pydantic models for the ``classify-axis-auto`` composite scaffold.

``classify-axis-auto`` collapses the deterministic head of the
``hpc-classify-axis`` skill — preflight (``discover-runs`` + cache-check +
``recall``) → the fast-path AST matcher (``classify-axis-easy``) → the
``classify-axis`` recorder — into ONE call. The LLM makes one tool call
and only does work on the genuine long tail (an ``unclassifiable`` /
``function_not_found`` matcher verdict).

The input reuses the exact persisted-shape :class:`_DataAxisConfig` so a
caller-supplied ``data_axis`` (the interview / slash path) is validated
byte-identically to what ``axes.yaml`` enforces. The result is a
discriminated union over the two terminal shapes: a recorded
classification (``recorded: true``) or a hand-off to the LLM decision
tree (``needs_llm_tree: true``).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel

# Reuse the exact persisted-shape model so a caller-supplied data_axis is
# validated identically to the classify-axis recorder + axes.yaml.
from hpc_agent._wire.fixtures.axes import _DataAxisConfig


class ClassifyAxisAutoInput(BaseModel):
    """Inputs to the ``classify-axis-auto`` composite verb."""

    model_config = ConfigDict(extra="forbid", title="classify-axis-auto input")

    run_name: str | None = Field(
        default=None,
        description=(
            "The @register_run function to classify. When omitted, the "
            "composite resolves it from discover-runs: if exactly one run "
            "exists it is used; if several exist, the composite returns "
            "spec_invalid (ambiguous_run) listing the candidates."
        ),
    )
    data_axis: _DataAxisConfig | None = Field(
        default=None,
        description=(
            "A caller-resolved classification (the interview / slash path, "
            "after a human-facing dialog). When present the composite "
            "records it directly as classified_by='interview' and runs "
            "neither recall nor the matcher."
        ),
    )
    root: str | None = Field(
        default=None,
        description=(
            "Experiments root forwarded to the preflight's recall sub-call. "
            "When omitted, recall falls back to "
            "~/.hpc-agent/config.json:experiment_roots."
        ),
    )
    task_kind: str | None = Field(
        default=None,
        description="Forwarded to the preflight's recall --task-kind to scope prior campaigns.",
    )


class _RecordedResult(BaseModel):
    """Terminal shape A–D: a classification was recorded into axes.yaml."""

    model_config = ConfigDict(extra="forbid")

    recorded: Literal[True] = Field(
        description="Discriminator: the classification was recorded into .hpc/axes.yaml.",
    )
    run_name: str = Field(min_length=1, description="The resolved @register_run function name.")
    kind: Literal["independent", "associative", "bounded_halo", "sequential", "cartesian"] = Field(
        description="The recorded DataAxis kind.",
    )
    classified_by: Literal["interview", "recall", "agent"] = Field(
        description=(
            "How the classification was reached: 'interview' (caller supplied "
            "data_axis), 'recall' (a prior similar experiment was reused), or "
            "'agent' (the AST matcher classified it autonomously)."
        ),
    )
    axes_path: str = Field(description="Absolute path to the axes.yaml the entry was written into.")


class _NeedsLlmTreeResult(BaseModel):
    """Terminal shape E: the matcher abstained; the LLM decision tree decides."""

    model_config = ConfigDict(extra="forbid")

    needs_llm_tree: Literal[True] = Field(
        description=(
            "Discriminator: the AST matcher abstained (unclassifiable / "
            "function_not_found); nothing was recorded. The caller walks the "
            "LLM decision tree and records via the classify-axis primitive."
        ),
    )
    run_name: str = Field(min_length=1, description="The resolved @register_run function name.")
    source_path: str = Field(
        min_length=1,
        description="Path to the source the LLM tree should read run_name's body from.",
    )
    run_signature_sha: str = Field(
        min_length=1,
        description="The run's current signature hash — passed to the classify-axis recorder.",
    )
    evidence: str = Field(description="The matcher's one-line evidence for why it abstained.")
    tried: list[str] = Field(
        description="The ordered pattern checks the matcher walked before abstaining.",
    )


# Discriminated union over the two terminal shapes. RootModel so a
# ``*Result``-suffixed BaseModel is discovered by build_schemas.py and
# emits an ``anyOf`` top-level schema in classify_axis_auto.output.json.
# The CLI dispatcher emits whichever shape the composite returns as the
# envelope ``data`` block.
class ClassifyAxisAutoResult(RootModel[_RecordedResult | _NeedsLlmTreeResult]):
    """Discriminated result: a recorded classification, or an LLM-tree hand-off."""
