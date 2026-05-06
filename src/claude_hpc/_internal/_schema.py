"""Centralized JSON-Schema validation with cross-file ``$ref`` resolution.

Every consumer that used to call :func:`jsonschema.validate` directly
must use :func:`validate` here so cross-file refs into
``envelope.json#/$defs/*`` resolve through the shared registry.

The registry is cached at module load and seeded with every
``claude_hpc/schemas/*.json`` file under both its ``$id`` (when
present) and a stable ``urn:claude-hpc:<filename>`` URI. New schemas
are picked up automatically the next process start; no per-call
plumbing.
"""

from __future__ import annotations

import functools
import json
from importlib.resources import files as _resource_files
from typing import Any


@functools.lru_cache(maxsize=1)
def schema_registry() -> Any:
    """Build the shared ``referencing.Registry`` once per process."""
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    registry = Registry()
    schemas_pkg = _resource_files("claude_hpc.schemas")
    for entry in schemas_pkg.iterdir():  # type: ignore[attr-defined]
        if not entry.name.endswith(".json"):
            continue
        try:
            doc = json.loads(entry.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        resource = Resource(contents=doc, specification=DRAFT202012)
        stable_uri = f"urn:claude-hpc:{entry.name}"
        registry = registry.with_resource(stable_uri, resource)
        doc_id = doc.get("$id") if isinstance(doc, dict) else None
        if isinstance(doc_id, str):
            registry = registry.with_resource(doc_id, resource)
    return registry


def validate(payload: Any, schema: dict) -> None:
    """Validate *payload* against *schema* using the shared registry.

    Raises :class:`jsonschema.ValidationError` on mismatch — the same
    exception the legacy ``jsonschema.validate(payload, schema)`` call
    raised, so existing callers' except-clauses keep working.
    """
    import jsonschema

    validator = jsonschema.Draft202012Validator(schema, registry=schema_registry())
    validator.validate(payload)
