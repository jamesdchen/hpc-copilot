"""Regenerate JSON Schemas under ``claude_hpc/schemas/`` from Pydantic models.

The wire SoT is the JSON file (every external consumer reads it).
The *authoring* SoT is the Pydantic model under
``claude_hpc/_schema_models/``. This script bridges the two: it
calls ``model.model_json_schema()`` (or ``adapter.json_schema()``
for root-array schemas) for every model auto-discovered under
``_schema_models/`` and writes / diffs the matching JSON file.

Same generator pattern as ``build_primitive_frontmatter.py``,
``build_primitive_index.py``, and ``build_operations_index.py``:
pre-commit + CI run ``--check`` so editing a Pydantic model without
regenerating the JSON is a CI failure.

Usage::

    uv run python scripts/build_schemas.py            # diff
    uv run python scripts/build_schemas.py --check    # CI gate
    uv run python scripts/build_schemas.py --write    # apply

Discovery rules
---------------

For each non-private submodule of ``claude_hpc._schema_models``:

1. Hardcoded mapping (``_NON_SUFFIX_MAPPING``) handles cross-cutting
   shapes whose names don't fit the suffix convention — the three
   ``TypeAdapter`` instances (``EnvelopeAdapter``, ``CampaignAdapter``,
   ``StagesAdapter``) and two persisted-data ``BaseModel`` shapes
   (``AxesConfig``, ``CampaignManifest``).
2. Every other public ``BaseModel`` subclass *defined in that module*
   (re-imports from sibling modules are skipped via ``__module__``
   check) is discovered by name suffix:

   * ``*Spec`` / ``*Input``    → ``<snake>.input.json``
   * ``*Result`` / ``*Report`` / ``*Envelope`` → ``<snake>.output.json``
   * Any other suffix          → skipped (treat as helper).

Style policy: emit whatever Pydantic v2 produces (``anyOf`` for
nullables, auto-titles per field, etc.). The wire validators and
LLM consumers don't care about cosmetic differences; chasing
byte-equality with hand-authored schemas isn't worth a custom
``GenerateJsonSchema`` subclass. The script does inject ``$schema``,
``$id``, and reorder the top-level keys into the conventional layout.
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

# Cross-cutting shapes whose names don't fit the *Spec/*Result suffix
# convention. Anything in this map is discovered verbatim regardless
# of type — this is also how TypeAdapter instances get registered (they
# have no class-name suffix to dispatch on).
_NON_SUFFIX_MAPPING: dict[str, str] = {
    "EnvelopeAdapter": "envelope.json",
    "CampaignAdapter": "campaign.output.json",
    "StagesAdapter": "stages.input.json",
    "AxesConfig": "axes.json",
    "CampaignManifest": "campaign_manifest.json",
}

# (suffix, output-side) pairs applied in order. The first match wins.
_SUFFIX_RULES: tuple[tuple[str, str], ...] = (
    ("Spec", "input"),
    ("Input", "input"),
    ("Result", "output"),
    ("Report", "output"),
    ("Envelope", "output"),
)

# Pydantic helpers we never want as standalone schemas.
_HELPER_NAMES: frozenset[str] = frozenset({"SuccessEnvelope", "ErrorEnvelope"})


_PASCAL_RE_1 = re.compile(r"(.)([A-Z][a-z]+)")
_PASCAL_RE_2 = re.compile(r"([a-z0-9])([A-Z])")


def _pascal_to_snake(name: str) -> str:
    """Convert PascalCase to snake_case (e.g. BestSubmitWindow -> best_submit_window)."""
    return _PASCAL_RE_2.sub(r"\1_\2", _PASCAL_RE_1.sub(r"\1_\2", name)).lower()


def _filename_for(obj: Any, attr_name: str, owning_module: str) -> str | None:
    """Return the schema filename for *obj*, or ``None`` to skip it.

    Discovery order:

    1. Cross-cutting names (``_NON_SUFFIX_MAPPING``) — verbatim, regardless
       of object type. This is how TypeAdapters get into the registry.
    2. ``BaseModel`` subclass defined in this module (skip re-imports)
       with a recognised suffix → ``<snake>.<side>.json``.
    3. Anything else → ``None`` (helper / unrelated import).
    """
    if attr_name in _NON_SUFFIX_MAPPING:
        return _NON_SUFFIX_MAPPING[attr_name]
    if not (isinstance(obj, type) and issubclass(obj, BaseModel) and obj is not BaseModel):
        return None
    if obj.__name__ in _HELPER_NAMES:
        return None
    # Ignore re-imports (only the module that *defines* the class wins).
    if getattr(obj, "__module__", None) != owning_module:
        return None
    for suffix, side in _SUFFIX_RULES:
        if obj.__name__.endswith(suffix):
            base = obj.__name__[: -len(suffix)]
            return f"{_pascal_to_snake(base)}.{side}.json"
    return None


def _build_schema_registry() -> list[tuple[type[BaseModel] | TypeAdapter[Any], str]]:
    """Discover every (model, filename) pair in ``_schema_models/``.

    Walks non-private submodules with :func:`pkgutil.iter_modules`; for
    each, inspects only the symbols *defined in that module* and
    applies :func:`_filename_for`. Returned list is sorted by filename
    so callers see a stable order.
    """
    pkg = claude_hpc._schema_models

    # Walk recursively so subpackages (workflows/, validators/,
    # fixtures/, queries/, actions/) are picked up alongside any
    # top-level helpers. ``walk_packages`` recurses into every
    # non-private submodule and subpackage in one pass.
    discovered: dict[str, tuple[Any, str]] = {}
    for _finder, modname, _ispkg in pkgutil.walk_packages(
        pkg.__path__,
        prefix=f"{pkg.__name__}.",
    ):
        leaf = modname.rsplit(".", 1)[-1]
        if leaf.startswith("_"):
            continue
        mod = importlib.import_module(modname)
        for attr_name in dir(mod):
            if attr_name.startswith("_"):
                continue
            obj = getattr(mod, attr_name)
            fname = _filename_for(obj, attr_name, owning_module=mod.__name__)
            if fname is None:
                continue
            if attr_name in discovered:
                # First-seen-wins silently hides re-exports; surface
                # collisions so a misregistered name is visible
                # instead of producing the wrong schema.
                prior_obj, prior_fname = discovered[attr_name]
                if prior_obj is not obj or prior_fname != fname:
                    raise RuntimeError(
                        f"schema name collision: {attr_name!r} defined in "
                        f"both {prior_obj.__module__} (→ {prior_fname}) and "
                        f"{mod.__name__} (→ {fname})"
                    )
                continue
            discovered[attr_name] = (obj, fname)

    return sorted(discovered.values(), key=lambda pair: pair[1])


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
            path.parent.mkdir(parents=True, exist_ok=True)
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
