"""Pydantic model for the ``capabilities`` query atom's output."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from claude_hpc._schema_models._shared import Scheduler


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
    python: str | None = None
    input_schema: str | None = None
    output_schema: str | None = None
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


class CapabilitiesResult(BaseModel):
    model_config = ConfigDict(extra="forbid", title="capabilities output data")

    version: str
    subcommands: list[str]
    supported_schedulers: list[Scheduler]
    schemas_dir: str
    journal_dir: str
    ssh_multiplexing: bool
    mars_skill_paths: dict[str, str] = Field(
        description=(
            "Map of slash-command skill bundle basename → absolute path "
            "to its SKILL.md, for the bundles shipped in the source "
            "tree (`skills/hpc-*/SKILL.md`). Empty on wheel-only "
            "installs. The field name is part of the wire contract; "
            "renaming it would break consumers that read it by key."
        ),
    )
    required_env: list[str]
    cluster_yaml_keys: list[_ClusterYamlKey] | None = Field(
        default=None,
        description=(
            "Declarative manifest of per-cluster fields recognized in "
            "clusters.yaml. Lets a campus user discover the schema by "
            "inspection without reading claude_hpc/infra/clusters.py "
            "source. Each item describes one validated field."
        ),
    )
    operations: list[_OperationCatalogEntry] | None = Field(
        default=None,
        description=(
            "Per-operation catalog. The CLI subcommands listed in "
            "`subcommands` are the invocable surface; this `operations` "
            "block carries machine-readable metadata about each one "
            "(verb tier, idempotency, side-effect class, schema files). "
            "Source-tree installs read from docs/primitives/ "
            "frontmatter; future wheel installs will read a baked "
            "operations.json shipped in the package."
        ),
    )
