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

import hpc_mapreduce

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["operations_catalog", "schema_for"]

_PACKAGE_ROOT = hpc_mapreduce._PACKAGE_ROOT


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
    if not cli.startswith("hpc-mapreduce "):
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

    Order: stable, sorted by (verb, name) so consumers can diff.
    Empty list when neither frontmatter source nor baked operations.json
    is available — callers should treat this as a not-yet-baked wheel.
    """
    prims_dir = _primitives_dir()
    if prims_dir is not None:
        return _from_frontmatters(prims_dir)
    baked = _baked_path()
    if baked.is_file():
        loaded = json.loads(baked.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, list) else []
    return []


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
