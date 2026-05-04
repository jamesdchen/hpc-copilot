"""Runtime catalog of the framework's operations.

Reads `docs/primitives/*.md` frontmatter once per call and returns a list
of dicts describing each operation: its verb tier, idempotency, side
effects, CLI invocation, Python entry point, and the schema files
that pin its input/output shapes (where they exist).

Used by :func:`hpc_mapreduce.agent_cli.cmd_capabilities` to expose the
operation catalog over the JSON envelope, so external agents can
discover what's invokable without reading any docs. The same data
drives `docs/operations.md` via `scripts/build_operations_index.py`.

For source-tree installs, frontmatters live next to the package at
`<repo_root>/docs/primitives/`. For future wheel installs, this module
will fall through to a baked `operations.json` shipped in the package
(not yet implemented; tracked as a packaging TODO).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import yaml

import claude_hpc
from claude_hpc._internal._primitive import get_registry

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["operations_catalog", "render_llms_full", "schema_for"]

_PACKAGE_ROOT = claude_hpc._PACKAGE_ROOT


def _primitives_dir() -> Path | None:
    """Locate `docs/primitives/` from the package root.

    Source-tree installs: `<repo>/hpc_mapreduce/` is the package, so the
    parent is `<repo>/` and frontmatters live at `<repo>/docs/primitives/`.
    Wheel installs don't ship docs/; this returns None and callers fall
    through to the baked operations.json (when implemented).
    """
    candidate = _PACKAGE_ROOT.parent / "docs" / "primitives"
    return candidate if candidate.is_dir() else None


def _baked_path() -> Path:
    """Path the baked operations JSON would live at in a wheel install."""
    return _PACKAGE_ROOT / "operations.json"


def _parse_frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    end = text.find("\n---\n", 4)
    fm = yaml.safe_load(text[4:end]) if end != -1 else None
    return fm if isinstance(fm, dict) else {}


def _cli_subcommand(backed_by: dict) -> str | None:
    cli = backed_by.get("cli", "") if isinstance(backed_by, dict) else ""
    if not cli or not cli.startswith("hpc-mapreduce "):
        return None
    rest = cli[len("hpc-mapreduce ") :].strip()
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


def _summarize_side_effects(side_effects: list) -> list[str]:
    """One-token-per-effect summary; e.g. ['rsyncs', 'submits', 'writes']."""
    if not side_effects:
        return []
    out: set[str] = set()
    for entry in side_effects:
        if isinstance(entry, dict):
            out.add(next(iter(entry)))
        else:
            out.add(str(entry).split(":", 1)[0].strip())
    return sorted(out)


def operations_catalog() -> list[dict[str, Any]]:
    """Return the operation catalog as a list of dicts.

    Each entry: ``{name, verb, idempotent, side_effects, cli, python,
    input_schema, output_schema}``. Missing schemas are reported as
    ``None`` (not absent) so callers can distinguish "no schema" from
    "field not present in this entry."

    Source-of-truth chain (C′):

    1. The ``@primitive`` registry (``claude_hpc._internal._primitive.get_registry``).
       Decorator metadata is the canonical SoT; this path is taken
       whenever any primitives have been registered.
    2. Frontmatter under ``docs/primitives/*.md``. Used to fill in
       any primitives missing from the registry (migration safety
       net — emits ``UserWarning`` so missing decorations get caught).
    3. Baked ``operations.json`` for wheel installs that ship without
       ``docs/`` on the file system.

    Order: stable, sorted by (verb, name) so consumers can diff.
    """
    # C′-v2 step 4: registry is the only source of truth. The previous
    # frontmatter fallback existed during migration; primitives without
    # decorators are now treated as orphans and ignored. Since the
    # frontmatter generator (scripts/build_primitive_frontmatter.py)
    # writes from the registry, an orphan frontmatter file is a
    # build-process bug, not a primitive worth surfacing here.
    registry_entries = _from_registry()
    if registry_entries:
        return sorted(registry_entries, key=lambda o: (o["verb"], o["name"]))

    baked = _baked_path()
    if baked.is_file():
        loaded = json.loads(baked.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, list) else []
    return []


def _from_registry() -> list[dict[str, Any]]:
    """Project the @primitive registry into the operations_catalog shape.

    The registry stores ``PrimitiveMeta`` objects; the catalog wants
    plain dicts. Field correspondence:

    * ``name``, ``verb``, ``idempotent`` map directly.
    * ``side_effects`` is summarized to one token per kind, mirroring
      :func:`_summarize_side_effects` so frontmatter fallback entries
      remain shape-compatible.
    * ``cli`` and ``python`` are derived: ``python`` is
      ``f"{func.__module__}.{func.__qualname__}"``; ``cli`` is read
      from the existing frontmatter (the registry doesn't yet carry
      CLI invocations — that's a follow-up).
    * ``input_schema`` / ``output_schema`` resolve via :func:`schema_for`
      using a synthetic ``backed_by`` dict.
    """
    out: list[dict[str, Any]] = []
    for meta in get_registry().values():
        backed = {
            "python": f"{meta.func.__module__}.{meta.func.__qualname__}",
            "cli": _cli_for_registry_entry(meta.name),
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
            }
        )
    return sorted(out, key=lambda o: (o["verb"], o["name"]))


def _cli_for_registry_entry(name: str) -> str | None:
    """Look up the ``cli:`` field from the primitive's frontmatter.

    The registry decorator doesn't yet carry the CLI invocation string
    (callers compose it themselves from argparse); we still read it
    from the frontmatter as a presentation hint. Returns None if the
    frontmatter is unavailable or has no cli field.
    """
    prims_dir = _primitives_dir()
    if prims_dir is None:
        return None
    path = prims_dir / f"{name}.md"
    if not path.is_file():
        return None
    fm = _parse_frontmatter(path)
    backed = fm.get("backed_by") if isinstance(fm.get("backed_by"), dict) else None
    if backed is None:
        return None
    cli = backed.get("cli")
    return cli if isinstance(cli, str) else None


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
    path = _PACKAGE_ROOT.parent / rel
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
    """Render the full claude-hpc API surface as one plain-text blob.

    Modeled on Modal\'s ``llms-full.txt`` pattern: one CLI invocation
    dumps the entire API surface (catalog table + every primitive\'s doc
    + every primitive\'s input/output schema + the envelope contract +
    boundary-contract + cli-spec) so an agent harness can load the whole
    context in a single read.

    Returns plain text suitable for human reading or LLM context loading
    --- NOT the JSON envelope. ``hpc-mapreduce capabilities --full`` is
    documented as an explicit human-mode flag analogous to ``--help``.
    """
    catalog = operations_catalog()
    parts: list[str] = []
    parts.append("# claude-hpc llms-full\n")
    parts.append(f"_version: {claude_hpc.__version__}_\n")

    parts.append("\n## Catalog\n\n")
    parts.append(_format_catalog_table(catalog))
    parts.append("\n")

    prims_dir = _primitives_dir()
    if prims_dir is not None:
        for entry in catalog:
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
    parts.append(_read_doc_file("docs/boundary-contract.md"))

    parts.append("\n## CLI spec\n\n")
    parts.append(_read_doc_file("docs/cli-spec.md"))

    return "".join(parts)


def _from_frontmatters(prims_dir: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in sorted(prims_dir.glob("*.md")):
        if path.name == "README.md":
            continue
        fm = _parse_frontmatter(path)
        if not fm or "name" not in fm:
            continue
        backed = fm.get("backed_by", {}) if isinstance(fm.get("backed_by"), dict) else {}
        out.append(
            {
                "name": fm["name"],
                "verb": fm.get("verb", "query"),
                "idempotent": bool(fm.get("idempotent", False)),
                "side_effects": _summarize_side_effects(fm.get("side_effects", [])),
                "cli": backed.get("cli"),
                "python": backed.get("python"),
                "input_schema": schema_for(fm["name"], "input", backed),
                "output_schema": schema_for(fm["name"], "output", backed),
            }
        )
    return sorted(out, key=lambda o: (o["verb"], o["name"]))
