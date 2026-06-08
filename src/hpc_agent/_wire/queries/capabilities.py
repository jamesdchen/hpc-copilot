"""Pydantic model for the ``capabilities`` query atom's output."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import Scheduler
from hpc_agent._wire.plugin_manifest import PluginManifest


class _ClusterYamlKey(BaseModel):
    """One declared field in clusters.yaml. Loose shape — extra info allowed."""

    model_config = ConfigDict(extra="allow")

    key: str
    type: str
    required: bool
    description: str
    default: Any | None = None


class _OperationCatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    verb: Literal["query", "validate", "mutate", "submit", "scaffold", "workflow"]
    idempotent: bool
    side_effects: list[str]
    cli: str | None = None
    agent_facing: bool | None = Field(
        default=None,
        description=(
            "Whether the LLM/agent calls this primitive directly. "
            "Workflows, scaffolds, validators, and atoms slash-commands "
            "or skills link to are true; framework internals composed "
            "inside workflows (e.g. poll-run-status inside monitor-flow) "
            "are false. Consumers (e.g. render_llms_full) tier their "
            "output by this flag so the agent context budget only pays "
            "for primitives the agent calls directly."
        ),
    )

    # NOTE: this bootstrap row is deliberately thin (#306). Its field set
    # is single-sourced as `operations.BOOTSTRAP_FIELDS` (the projection
    # `operations_bootstrap()` uses); `test_bootstrap_fields_match_wire_model`
    # pins these model fields to that tuple, so the thin shape is defined
    # once, not re-stated here. The forensic pointers (`python`,
    # `input_schema`, `output_schema`) and the one-line `summary` are NOT
    # carried — they live in the full `operations_catalog()` row and are
    # fetched on demand via `hpc-agent describe <name>` (full contract) or
    # `hpc-agent find "<intent>"` (thin {name,verb,cli,summary} rows).


class CapabilitiesResult(BaseModel):
    model_config = ConfigDict(extra="forbid", title="capabilities output data")

    version: str
    subcommands: list[str]
    supported_schedulers: list[Scheduler]
    schemas_dir: str
    journal_dir: str
    ssh_multiplexing: bool
    required_env: list[str]
    cluster_yaml_keys: list[_ClusterYamlKey] | None = Field(
        default=None,
        description=(
            "Declarative manifest of per-cluster fields recognized in "
            "clusters.yaml. Lets a campus user discover the schema by "
            "inspection without reading hpc_agent/infra/clusters.py "
            "source. Each item describes one validated field."
        ),
    )
    operations: list[_OperationCatalogEntry] | None = Field(
        default=None,
        description=(
            "Per-operation catalog — the thin bootstrap row for every "
            "primitive: name, verb tier, idempotency, side-effect class, "
            "CLI invocation, and whether it's agent-facing. The CLI "
            "subcommands listed in `subcommands` are the invocable "
            "surface; this block adds the machine-readable flags an "
            "orchestrator gates on. Heavier content — schema-file "
            "pointers, the Python entry point, the one-line summary, and "
            "the full doc body — is intentionally NOT inlined here (#306): "
            'fetch it on demand with `hpc-agent find "<intent>"` (thin '
            "search) or `hpc-agent describe <name>` (one full contract)."
        ),
    )
    plugins: list[PluginManifest] = Field(
        default_factory=list,
        description=(
            "Self-declared manifest of every installed hpc-agent plugin "
            "(Item 5). Projected from each plugin's top-level "
            "``MANIFEST = PluginManifest(...)``; absent for plugins that "
            "haven't yet declared one (the loader emits a "
            "DeprecationWarning in that case). Empty when no plugin is "
            "installed."
        ),
    )
