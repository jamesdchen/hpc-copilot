"""Envelope-data models for the merged ``trace`` query verb.

``trace`` is one verb with three modes:

* the default **lineage** query (``--campaign-id`` / ``--run-id`` /
  ``--format``) joins the per-run journal records, the per-run sidecars, and
  the signable provenance manifest into one *derived* execution DAG — the
  "explain exactly what produced this result, and in what order" surface,
  recomputed from disk on every call (like ``provenance-manifest``) so it is
  always consistent with the runs on disk rather than a second source of
  truth that can drift;
* ``--spec {"mode": "render", ...}`` — the four data-trace projections
  (``docs/design/data-trace.md`` T5), implemented in
  :mod:`hpc_agent.ops.trace_render_op`;
* ``--spec {"mode": "diff", ...}`` — the two-trace first-divergence overlay
  (data-trace Projection 5), implemented in
  :mod:`hpc_agent.ops.trace_diff_op`.

The render/diff spec + result shapes stay authored in their own wire modules
(:mod:`hpc_agent._wire.queries.trace_render` /
:mod:`hpc_agent._wire.queries.trace_diff` — the data-trace design pins live
against those files); this module only wraps them into the merged verb's
``trace.input.json`` / ``trace.output.json`` union. The wrapped classes are
listed in ``scripts/build_schemas.py``'s embedded-sub-model skip set so they
no longer emit standalone per-verb schema files.

The lineage node/edge payloads are kept as ``list[dict[str, Any]]`` (the same
loose shape ``campaign.output.json`` uses for per-iteration history): nodes
are heterogeneous by ``kind`` (``campaign`` / ``run`` / ``wave``) and over-
constraining them in the wire schema would buy nothing — the consumer
dispatches on ``kind``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel

from hpc_agent._wire.queries.trace_diff import TraceDiffResult, TraceDiffSpec
from hpc_agent._wire.queries.trace_render import TraceRenderResult, TraceRenderSpec


class TraceLineageResult(BaseModel):
    """Returned by ``hpc-agent trace --campaign-id <id>`` / ``--run-id <id>``."""

    model_config = ConfigDict(extra="forbid", title="trace output")

    trace_schema_version: int = Field(
        ge=1,
        description="Bumped when the emitted node/edge shape changes incompatibly.",
    )
    scope: Literal["campaign", "run"] = Field(
        description="Whether the DAG was rooted at a campaign or a single run's lineage.",
    )
    format: Literal["dag", "flat", "dot"] = Field(
        description=(
            "`dag` includes edges + per-wave nodes; `flat` is the run list "
            "only; `dot` is the full dag plus a rendered Graphviz `dot` string."
        ),
    )
    campaign_id: str | None = Field(
        default=None,
        description="The campaign tag, when scope == 'campaign'; null for run scope.",
    )
    root: str = Field(
        min_length=1,
        description="Node id of the DAG root (`campaign:<id>` or `run:<seed>`).",
    )
    signature: str | None = Field(
        default=None,
        description=(
            "The provenance-manifest self-attesting signature for this campaign "
            "(64-hex). Campaign scope only — null for run scope."
        ),
    )
    node_count: int = Field(ge=0, description="Number of nodes in `nodes`.")
    nodes: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Heterogeneous nodes keyed by `kind`: a `campaign` node, one `run` "
            "node per run (carrying its provenance fingerprint + lifecycle "
            "status + timing), and one `wave` node per wave (dag format only)."
        ),
    )
    edges: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Directed edges by `rel`: `member` (run→campaign membership), "
            "`derived-from` (run→parent run lineage via parent_run_ids), and "
            "`contains` (run→wave). Empty in `flat` format."
        ),
    )
    dot: str | None = Field(
        default=None,
        description=(
            "Graphviz DOT rendering of the DAG — populated only in `dot` "
            "format, null otherwise. Pipe it to `dot -Tsvg` to draw the graph."
        ),
    )


class TraceRenderProjection(TraceRenderSpec):
    """The render mode's spec: :class:`TraceRenderSpec` plus the discriminator."""

    mode: Literal["render"]


class TraceDiffProjection(TraceDiffSpec):
    """The diff mode's spec: :class:`TraceDiffSpec` plus the discriminator."""

    mode: Literal["diff"]


class TraceSpec(
    RootModel[Annotated[TraceRenderProjection | TraceDiffProjection, Field(discriminator="mode")]]
):
    """The merged verb's optional ``--spec``: a mode-discriminated projection.

    The bare lineage query takes no spec (``--campaign-id`` / ``--run-id``
    args only); ``mode: render`` / ``mode: diff`` select the data-trace
    projections. Emitted as ``trace.input.json``.
    """


class TraceResult(RootModel[TraceLineageResult | TraceDiffResult | TraceRenderResult]):
    """The merged verb's output union — one shape per mode.

    Emitted as ``trace.output.json``; the runtime ``validate_output`` gate and
    external consumers validate whichever mode's payload was returned against
    this union.
    """
