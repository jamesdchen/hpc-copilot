"""Regenerate JSON Schemas under ``claude_hpc/schemas/`` from Pydantic models.

The wire SoT is the JSON file (every external consumer reads it).
The *authoring* SoT is the Pydantic model under
``claude_hpc/_schema_models/``. This script bridges the two: it
calls ``model.model_json_schema()`` for every entry in
``SCHEMA_REGISTRY`` and writes / diffs the matching JSON file.

Same generator pattern as ``build_primitive_frontmatter.py``,
``build_primitive_index.py``, and ``build_operations_index.py``:
pre-commit + CI run ``--check`` so editing a Pydantic model without
regenerating the JSON is a CI failure.

Usage::

    uv run python scripts/build_schemas.py            # diff
    uv run python scripts/build_schemas.py --check    # CI gate
    uv run python scripts/build_schemas.py --write    # apply

Spike scope: only ``submit-flow`` models. To migrate another atom,
add a ``(Model, "name.kind.json")`` row to ``SCHEMA_REGISTRY``,
delete the hand-authored JSON if you want a clean diff, then run
``--write``.
"""

from __future__ import annotations

import difflib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pydantic import BaseModel  # noqa: E402

from claude_hpc._schema_models.submit_flow import (  # noqa: E402
    SubmitFlowResult,
    SubmitFlowSpec,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = REPO_ROOT / "src" / "claude_hpc" / "schemas"


# Each entry: (Pydantic model, schema filename, "$id" URL fragment).
# The $id is the canonical URL the wire schema announces; mirroring
# the existing pattern keeps cross-file ``$ref`` consumers happy.
SCHEMA_REGISTRY: list[tuple[type[BaseModel], str, str]] = [
    (
        SubmitFlowSpec,
        "submit_flow.input.json",
        "https://github.com/jamesdchen/claude-hpc/schemas/submit_flow.input.json",
    ),
    (
        SubmitFlowResult,
        "submit_flow.output.json",
        "https://github.com/jamesdchen/claude-hpc/schemas/submit_flow.output.json",
    ),
]


def _normalize(schema: dict, schema_id: str, model: type[BaseModel]) -> dict:
    """Tweak Pydantic's emitted schema to match the project's house style.

    Pydantic v2 emits a draft-2020-12 schema with no ``$schema``
    declaration and no ``$id``; the project's hand-authored files
    carry both. The model's docstring becomes ``description``;
    ``model_config['title']`` becomes ``title``. We also reorder
    top-level keys so the diff against the hand-authored JSON stays
    readable (``$schema, $id, title, description, type, required,
    additionalProperties, properties``).
    """
    schema = dict(schema)
    schema.setdefault("$schema", "https://json-schema.org/draft/2020-12/schema")
    schema["$id"] = schema_id
    if "description" not in schema and model.__doc__:
        schema["description"] = " ".join(model.__doc__.split())
    # Top-level key order mirrors the hand-authored convention.
    preferred_order = (
        "$schema",
        "$id",
        "title",
        "description",
        "type",
        "required",
        "additionalProperties",
        "properties",
        "$defs",
    )
    ordered = {k: schema[k] for k in preferred_order if k in schema}
    for k, v in schema.items():
        if k not in ordered:
            ordered[k] = v
    return ordered


def _emit(model: type[BaseModel], schema_id: str) -> str:
    schema = model.model_json_schema()
    schema = _normalize(schema, schema_id, model)
    return json.dumps(schema, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    write = "--write" in sys.argv
    check = "--check" in sys.argv

    drift: list[tuple[Path, str, str]] = []  # (path, old, new)
    for model, fname, schema_id in SCHEMA_REGISTRY:
        path = SCHEMAS_DIR / fname
        new = _emit(model, schema_id)
        old = path.read_text(encoding="utf-8") if path.is_file() else ""
        if old != new:
            drift.append((path, old, new))

    if not drift:
        print(f"schemas up to date ({len(SCHEMA_REGISTRY)} models)")
        return 0

    if check:
        print(
            f"ERROR: {len(drift)} schema file(s) out of date — "
            "run scripts/build_schemas.py --write to regenerate",
            file=sys.stderr,
        )
        for path, _, _ in drift:
            print(f"  {path.relative_to(REPO_ROOT)}", file=sys.stderr)
        return 1

    if write:
        for path, _, new in drift:
            path.write_text(new, encoding="utf-8")
            print(f"  wrote {path.relative_to(REPO_ROOT)}")
        print(f"regenerated {len(drift)} schema file(s)")
        return 0

    # Default: print a diff so the human can preview without writing.
    for path, old, new in drift:
        rel = path.relative_to(REPO_ROOT)
        print(f"--- a/{rel}")
        print(f"+++ b/{rel}")
        diff = difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            n=3,
        )
        sys.stdout.write("".join(diff))
    return 0


if __name__ == "__main__":
    sys.exit(main())
