"""Envelope-data model for the ``trace`` query verb.

``trace`` joins the per-run journal records, the per-run sidecars, and the
signable provenance manifest into one *derived* execution DAG — the
"explain exactly what produced this result, and in what order" surface. The
trace is recomputed from disk on every call (like ``provenance-manifest``),
so it is always consistent with the runs on disk rather than a second source
of truth that can drift.

The node/edge payloads are kept as ``list[dict[str, Any]]`` (the same loose
shape ``campaign.output.json`` uses for per-iteration history): nodes are
heterogeneous by ``kind`` (``campaign`` / ``run`` / ``wave``) and over-
constraining them in the wire schema would buy nothing — the consumer
dispatches on ``kind``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class TraceResult(BaseModel):
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
