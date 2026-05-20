"""Runtime catalog of the framework's operations.

Projects the in-process ``@primitive`` registry into a list of dicts
describing each operation: its verb tier, idempotency, side effects,
CLI invocation, Python entry point, and the schema files that pin its
input/output shapes (where they exist).

Used by :func:`hpc_agent.agent_cli.cmd_capabilities` to expose the
operation catalog over the JSON envelope, so external agents can
discover what's invokable without reading any docs. The same data
drives ``docs/generated/operations.md`` via
``scripts/build_operations_index.py``.

The in-process ``@primitive`` registry is the only source of truth.
``scripts/bake_operations_json.py`` writes a redundant snapshot to
``src/hpc_agent/operations.json`` for diff/discoverability and for
the docs-build cross-check, but the runtime catalog is always derived
from the live registry — never from the baked file.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import hpc_agent
from hpc_agent._internal.primitive import get_registry

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["operations_catalog", "render_llms_full", "schema_for"]

_PACKAGE_ROOT = hpc_agent._PACKAGE_ROOT


def _primitives_dir() -> Path | None:
    """Locate `docs/primitives/` from the package root.

    Source-tree installs: `<repo>/src/hpc_agent/` is the package, so the
    repo root is two levels up and frontmatters live at
    `<repo>/docs/primitives/`. Wheel installs don't ship docs/; this
    returns None and ``render_llms_full`` skips the per-primitive prose
    block (the catalog table + schemas still render).
    """
    candidate = _PACKAGE_ROOT.parent.parent / "docs" / "primitives"
    return candidate if candidate.is_dir() else None


def _cli_subcommand(backed_by: dict) -> str | None:
    cli = backed_by.get("cli", "") if isinstance(backed_by, dict) else ""
    if not cli or not cli.startswith("hpc-agent "):
        return None
    rest = cli[len("hpc-agent ") :].strip()
    tokens = rest.split(" ")
    if len(tokens) >= 2 and tokens[1] in {"list", "describe", "status"}:
        return f"{tokens[0]}_{tokens[1]}"
    return tokens[0] if tokens else None


def schema_for(name: str, side: str, backed_by: dict) -> str | None:
    """Return the schema filename for a primitive, if one exists."""
    candidates = [
        f"{name.replace('-', '_')}.{side}.json",
        f"{name}.{side}.json",
    ]
    cli_name = _cli_subcommand(backed_by)
    if cli_name:
        candidates.append(f"{cli_name.replace('-', '_')}.{side}.json")
    schemas_dir = _PACKAGE_ROOT / "schemas"
    for fname in candidates:
        if (schemas_dir / fname).is_file():
            return fname
    return None


def operations_catalog() -> list[dict[str, Any]]:
    """Return the operation catalog as a list of dicts.

    Each entry: ``{name, verb, idempotent, side_effects, cli, python,
    input_schema, output_schema}``. Missing schemas are reported as
    ``None`` (not absent) so callers can distinguish "no schema" from
    "field not present in this entry."

    Source of truth: the in-process ``@primitive`` registry
    (``hpc_agent._internal.primitive.get_registry``). Decorator
    metadata is the canonical SoT and the only source consulted at
    runtime — the baked ``src/hpc_agent/operations.json`` exists for
    diff/discoverability via ``scripts/bake_operations_json.py`` but
    is never read back.

    Order: stable, sorted by (verb, name) so consumers can diff.
    """
    return sorted(_from_registry(), key=lambda o: (o["verb"], o["name"]))


def _from_registry() -> list[dict[str, Any]]:
    """Project the @primitive registry into the operations_catalog shape.

    The registry stores ``PrimitiveMeta`` objects; the catalog wants
    plain dicts. Field correspondence:

    * ``name``, ``verb``, ``idempotent`` map directly.
    * ``side_effects`` is summarized to one token per kind (the
      :class:`SideEffect.kind` of each entry).
    * ``cli`` and ``python`` are derived: ``python`` is
      ``f"{func.__module__}.{func.__qualname__}"``; ``cli`` comes
      directly from the decorator's ``cli=`` kwarg (registry SoT).
    * ``input_schema`` / ``output_schema`` resolve via :func:`schema_for`
      using a synthetic ``backed_by`` dict.
    """
    out: list[dict[str, Any]] = []
    for meta in get_registry().values():
        backed = {
            "python": f"{meta.func.__module__}.{meta.func.__qualname__}",
            "cli": meta.cli,
        }
        out.append(
            {
                "name": meta.name,
                "verb": meta.verb,
                "idempotent": bool(meta.idempotent),
                "side_effects": sorted({s.kind for s in meta.side_effects}),
                "cli": backed["cli"],
                "python": backed["python"],
                "input_schema": schema_for(meta.name, "input", backed),
                "output_schema": schema_for(meta.name, "output", backed),
                "agent_facing": bool(meta.agent_facing),
            }
        )
    return sorted(out, key=lambda o: (o["verb"], o["name"]))


def _format_catalog_table(catalog: list[dict[str, Any]]) -> str:
    """Render the operations catalog as a fixed-width text table."""
    if not catalog:
        return "(no operations available)"
    headers = ("name", "verb", "idempotent", "side_effects", "cli")
    rows: list[tuple[str, str, str, str, str]] = []
    for entry in catalog:
        rows.append(
            (
                str(entry.get("name", "")),
                str(entry.get("verb", "")),
                "yes" if entry.get("idempotent") else "no",
                ",".join(entry.get("side_effects") or []) or "-",
                str(entry.get("cli") or "-"),
            )
        )
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    sep = "  ".join("-" * w for w in widths)
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)), sep]
    lines.extend("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in rows)
    return "\n".join(lines)


def _read_doc_file(rel: str) -> str:
    """Read a doc file relative to the repo root; ``"(missing)"`` if absent."""
    path = _PACKAGE_ROOT.parent.parent / rel
    if not path.is_file():
        return f"(missing: {rel})"
    return path.read_text(encoding="utf-8")


def _read_schema_file(name: str) -> str:
    """Pretty-print a schema JSON; ``"(missing)"`` if absent."""
    path = _PACKAGE_ROOT / "schemas" / name
    if not path.is_file():
        return f"(missing: schemas/{name})"
    try:
        return json.dumps(json.loads(path.read_text(encoding="utf-8")), indent=2, sort_keys=True)
    except json.JSONDecodeError:
        return path.read_text(encoding="utf-8")


def render_llms_full() -> str:
    """Render the hpc-agent API surface as one plain-text blob.

    Modeled on Modal's ``llms-full.txt`` pattern: one CLI invocation
    dumps the API surface so an agent harness can load context in a
    single read.

    Tiered to keep agent context budget honest. ``agent_facing=True``
    primitives — workflows, scaffolds, validators, plus the atoms
    skills / slash commands link to — ship their full body + input /
    output schemas. The remaining atoms are framework internals
    composed inside workflows (e.g. ``poll-run-status`` inside
    ``monitor-flow``); they appear in the catalog table above so
    agents can still introspect "what exists" and shell to their CLI
    for forensic access, but their per-primitive prose / schema block
    is omitted. The Composite property is about runtime invocation
    uniformity (Leaf and Composite share an envelope), not
    documentation surface — clients only need full context for the
    primitives they call directly.

    Returns plain text suitable for human reading or LLM context
    loading --- NOT the JSON envelope. ``hpc-agent capabilities
    --full`` is documented as an explicit human-mode flag analogous to
    ``--help``.
    """
    catalog = operations_catalog()
    parts: list[str] = []
    parts.append("# hpc-agent llms-full\n")
    parts.append(f"_version: {hpc_agent.__version__}_\n")

    parts.append("\n## Catalog\n\n")
    parts.append(_format_catalog_table(catalog))
    parts.append("\n")

    agent_facing = [e for e in catalog if e.get("agent_facing")]
    internal = [e for e in catalog if not e.get("agent_facing")]
    parts.append(
        f"\n_{len(agent_facing)} agent-facing primitives expanded below; "
        f"{len(internal)} framework-internal primitives appear in the catalog "
        "table only (composed transitively by workflows). Use "
        "``hpc-agent <subcommand> --help`` or read the schema file named "
        "in the catalog row for forensic access._\n"
    )

    prims_dir = _primitives_dir()
    if prims_dir is not None:
        for entry in agent_facing:
            name = entry["name"]
            parts.append(f"\n## Primitive: {name}\n\n")
            doc_path = prims_dir / f"{name}.md"
            if doc_path.is_file():
                parts.append(doc_path.read_text(encoding="utf-8"))
            else:
                parts.append(f"(no doc at docs/primitives/{name}.md)\n")
            input_schema = entry.get("input_schema")
            if input_schema:
                parts.append(f"\n### Input schema: {input_schema}\n\n```json\n")
                parts.append(_read_schema_file(input_schema))
                parts.append("\n```\n")
            output_schema = entry.get("output_schema")
            if output_schema:
                parts.append(f"\n### Output schema: {output_schema}\n\n```json\n")
                parts.append(_read_schema_file(output_schema))
                parts.append("\n```\n")

    parts.append("\n## Envelope\n\n```json\n")
    parts.append(_read_schema_file("envelope.json"))
    parts.append("\n```\n")

    parts.append("\n## Boundary contract\n\n")
    parts.append(_read_doc_file("docs/reference/boundary-contract.md"))

    parts.append("\n## CLI spec\n\n")
    parts.append(_read_doc_file("docs/reference/cli-spec.md"))

    return "".join(parts)
