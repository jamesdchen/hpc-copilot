"""Regenerate JSON Schemas under ``claude_hpc/schemas/`` from Pydantic models.

The wire SoT is the JSON file (every external consumer reads it).
The *authoring* SoT is the Pydantic model under
``claude_hpc/_schema_models/``. This script bridges the two: it
calls ``model.model_json_schema()`` (or ``adapter.json_schema()``
for root-array schemas) for every entry in ``SCHEMA_REGISTRY`` and
writes / diffs the matching JSON file.

Same generator pattern as ``build_primitive_frontmatter.py``,
``build_primitive_index.py``, and ``build_operations_index.py``:
pre-commit + CI run ``--check`` so editing a Pydantic model without
regenerating the JSON is a CI failure.

Usage::

    uv run python scripts/build_schemas.py            # diff
    uv run python scripts/build_schemas.py --check    # CI gate
    uv run python scripts/build_schemas.py --write    # apply

Style policy: emit whatever Pydantic v2 produces (``anyOf`` for
nullables, auto-titles per field, etc.). The wire validators and
LLM consumers don't care about cosmetic differences; chasing
byte-equality with hand-authored schemas isn't worth a custom
``GenerateJsonSchema`` subclass. The script does inject
``$schema``, ``$id``, and (when the model docstring or
``model_config['title']`` is present) reorder the top-level keys
into the conventional layout.
"""

from __future__ import annotations

import difflib
import importlib
import json
import pkgutil
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pydantic import BaseModel, TypeAdapter  # noqa: E402

import claude_hpc._schema_models  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = REPO_ROOT / "src" / "claude_hpc" / "schemas"

_ID_BASE = "https://github.com/jamesdchen/claude-hpc/schemas"


def _pascal_to_snake(name: str) -> str:
    """Convert PascalCase to snake_case."""
    s1 = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub("([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


def _build_schema_registry() -> list[tuple[type[BaseModel] | TypeAdapter[Any], str]]:
    """Auto-discover Pydantic models and TypeAdapters in _schema_models.

    Rules:
    1. Walk all submodules, import them.
    2. Collect every public BaseModel subclass and TypeAdapter instance.
    3. Skip helpers from _shared.py and anything starting with underscore.
    4. Derive filename from class/adapter name:
       - *Spec or *Input → <snake>.input.json
       - *Result or *Report → <snake>.output.json
       - Exception list (Adapters, special shapes) → hardcoded mappings
    5. Convert PascalCase → snake_case, stripping suffix before conversion.
    """
    # Exception mappings: class name to filename
    exception_map: dict[str, str] = {
        "EnvelopeAdapter": "envelope.json",
        "AxesConfig": "axes.json",
        "CampaignManifest": "campaign_manifest.json",
        "CampaignAdapter": "campaign.output.json",
        "StagesAdapter": "stages.input.json",
    }

    # Import all submodules
    pkg_path = Path(claude_hpc._schema_models.__file__).parent
    for _importer, modname, _ispkg in pkgutil.iter_modules([str(pkg_path)]):
        if modname.startswith("_"):
            continue
        importlib.import_module(f"claude_hpc._schema_models.{modname}")

    discovered: dict[str, tuple[Any, str]] = {}

    # Inspect all submodules and extract classes/adapters
    for name, mod in vars(claude_hpc._schema_models).items():
        if name.startswith("_") or not hasattr(mod, "__dict__"):
            continue

        # mod is actually a module
        for attr_name, obj in vars(mod).items():
            if attr_name.startswith("_"):
                continue

            # Check exception list first
            if attr_name in exception_map:
                discovered[attr_name] = (obj, exception_map[attr_name])
                continue

            # Check if it's a TypeAdapter instance (must come before BaseModel check)
            if isinstance(obj, TypeAdapter):
                # Only include if in exception map (already handled above)
                continue

            # Check if it's a BaseModel subclass
            if isinstance(obj, type) and issubclass(obj, BaseModel) and obj is not BaseModel:
                # Skip internal helper models (those starting with underscore or
                # internal envelope models that aren't top-level exports)
                if attr_name.startswith("_") or obj.__name__ in (
                    "SuccessEnvelope",
                    "ErrorEnvelope",
                ):
                    continue

                # Strip suffix and derive filename
                # Only include if it matches recognized suffixes
                suffix = None
                base_name = obj.__name__

                if obj.__name__.endswith("Spec"):
                    suffix = "input.json"
                    base_name = obj.__name__[:-4]
                elif obj.__name__.endswith("Input"):
                    suffix = "input.json"
                    base_name = obj.__name__[:-5]
                elif obj.__name__.endswith(("Result", "Report")):
                    suffix = "output.json"
                    base_name = obj.__name__[:-6]
                elif obj.__name__.endswith("Envelope"):
                    suffix = "output.json"
                    base_name = obj.__name__[:-8]
                else:
                    # No recognized suffix, skip (e.g., internal helper models)
                    continue

                snake_name = _pascal_to_snake(base_name)
                filename = f"{snake_name}.{suffix}"
                discovered[obj.__name__] = (obj, filename)

    # Build the registry, sorted by filename for stability
    registry: list[tuple[Any, str]] = list(discovered.values())
    return sorted(registry, key=lambda x: x[1])


SCHEMA_REGISTRY = _build_schema_registry()


def _emit_schema(model_or_adapter: Any) -> dict[str, Any]:
    """Call the right schema-emit method for either a BaseModel or a TypeAdapter."""
    if isinstance(model_or_adapter, TypeAdapter):
        return model_or_adapter.json_schema()  # type: ignore[no-any-return]
    if isinstance(model_or_adapter, type) and issubclass(model_or_adapter, BaseModel):
        return model_or_adapter.model_json_schema()
    raise TypeError(f"unexpected schema source: {model_or_adapter!r}")


def _normalize(schema: dict, schema_id: str) -> dict:
    """Inject ``$schema`` / ``$id`` and reorder top-level keys.

    Pydantic v2 emits a draft-2020-12 schema with no ``$schema``
    declaration and no ``$id``; the project's hand-authored files
    carry both. We add them and reorder the top-level keys so the
    diff stays readable.
    """
    schema = dict(schema)
    schema.setdefault("$schema", "https://json-schema.org/draft/2020-12/schema")
    schema["$id"] = schema_id
    preferred_order = (
        "$schema",
        "$id",
        "title",
        "description",
        "type",
        "required",
        "additionalProperties",
        "properties",
        "items",
        "minItems",
        "$defs",
    )
    ordered = {k: schema[k] for k in preferred_order if k in schema}
    for k, v in schema.items():
        if k not in ordered:
            ordered[k] = v
    return ordered


def _emit(model_or_adapter: Any, fname: str) -> str:
    schema = _emit_schema(model_or_adapter)
    schema = _normalize(schema, f"{_ID_BASE}/{fname}")
    return json.dumps(schema, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    write = "--write" in sys.argv
    check = "--check" in sys.argv

    drift: list[tuple[Path, str, str]] = []  # (path, old, new)
    for src, fname in SCHEMA_REGISTRY:
        path = SCHEMAS_DIR / fname
        try:
            new = _emit(src, fname)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: emitting {fname}: {exc!r}", file=sys.stderr)
            return 2
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
