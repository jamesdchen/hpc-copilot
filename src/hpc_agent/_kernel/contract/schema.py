"""Centralized JSON-Schema validation with cross-file ``$ref`` resolution.

Every consumer that used to call :func:`jsonschema.validate` directly
must use :func:`validate` here so any future cross-file refs resolve
through the shared registry.

Post-Pydantic-migration the per-primitive schemas are self-contained
(each model inlines what it needs from ``_schema_models/_shared.py``),
so cross-file refs are rare — but the registry stays so that
hand-authored payloads referencing ``envelope.json#/$defs/*`` from
older agents still resolve.

The registry is cached at module load and seeded with every
``hpc_agent/schemas/*.json`` file under both its ``$id`` (when
present) and a stable ``urn:hpc-agent:<filename>`` URI. New schemas
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
    from referencing import Registry
    from referencing.jsonschema import DRAFT202012

    registry = Registry()
    schemas_pkg = _resource_files("hpc_agent.schemas")
    for entry in schemas_pkg.iterdir():  # type: ignore[attr-defined]
        if not entry.name.endswith(".json"):
            continue
        try:
            doc = json.loads(entry.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        resource = DRAFT202012.create_resource(doc)
        stable_uri = f"urn:hpc-agent:{entry.name}"
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


# ---------------------------------------------------------------------------
# Producer-side output validation (CLI envelope ``data`` block)
# ---------------------------------------------------------------------------
#
# Switched on under pytest (autodetected) and any time
# ``CLAUDE_HPC_VALIDATE_OUTPUTS`` is set to a truthy value. In production
# the gate is off by default — outputs are validated in CI, not on the
# hot path. Failure surfaces as ``OutputSchemaDrift`` which the agent_cli
# error handler turns into an ``error_code=internal`` envelope.


class OutputSchemaDrift(RuntimeError):
    """Producer-side: a primitive emitted ``data`` that doesn't match its output schema."""


@functools.lru_cache(maxsize=1)
def _output_validation_enabled() -> bool:
    import os
    import sys

    if os.environ.get("CLAUDE_HPC_VALIDATE_OUTPUTS", "").lower() in {"1", "true", "yes"}:
        return True
    return "pytest" in sys.modules


@functools.lru_cache(maxsize=128)
def _output_schema_for(primitive_name: str) -> dict | None:
    """Load the output JSON schema for *primitive_name*, or None if absent.

    Uses the same 3-candidate ladder as
    :func:`hpc_agent._kernel.registry.operations.schema_for` so the runtime
    validator never disagrees with the docs/catalog about which file
    backs a given primitive — pre-v3 this only looked up
    ``<name>.output.json`` and silently no-op'd on every primitive
    whose schema is keyed off the CLI subcommand name (preflight,
    discover, status, submit, reconcile, runtime_prior;
    v3 BUG-1V3-1).
    """
    candidates = [
        f"{primitive_name.replace('-', '_')}.output.json",
        f"{primitive_name}.output.json",
    ]
    # Add the CLI-derived candidate from the primitive registry so
    # CLI-renamed primitives (e.g. ``check-preflight`` →
    # ``preflight.output.json``) resolve too.
    try:
        from hpc_agent._kernel.registry.operations import _cli_subcommand
        from hpc_agent._kernel.registry.primitive import get_registry

        meta = get_registry().get(primitive_name)
        if meta is not None and meta.cli:
            from hpc_agent.cli._dispatch import cli_to_invocation_string

            cli_str = cli_to_invocation_string(meta.name, meta.cli)
            if cli_str:
                cli_name = _cli_subcommand({"cli": cli_str})
                if cli_name:
                    candidates.append(f"{cli_name.replace('-', '_')}.output.json")
    except Exception:  # noqa: BLE001 — fallback ladder; never crash validation
        pass

    for fname in candidates:
        try:
            text = (_resource_files("hpc_agent.schemas") / fname).read_text(encoding="utf-8")
        except (FileNotFoundError, ModuleNotFoundError, OSError):
            continue
        try:
            loaded: dict = json.loads(text)
        except json.JSONDecodeError:
            continue
        return loaded
    return None


def validate_output(data: Any, primitive_name: str) -> None:
    """Validate envelope ``data`` against ``<primitive_name>.output.json``.

    No-op when validation is disabled (production default) or when the
    primitive has no output schema. Raises :class:`OutputSchemaDrift`
    on mismatch — the producer side made a mistake, not the consumer,
    so the error message points at the primitive name and the schema
    path that failed.
    """
    if not _output_validation_enabled():
        return
    schema = _output_schema_for(primitive_name)
    if schema is None:
        return
    try:
        validate(data, schema)
    except Exception as exc:  # jsonschema.ValidationError, but defensive
        path = ""
        absolute = getattr(exc, "absolute_path", None)
        if absolute is not None:
            path = "/".join(str(p) for p in absolute) or "<root>"
        raise OutputSchemaDrift(
            f"primitive {primitive_name!r} emitted data that doesn't match "
            f"{primitive_name.replace('-', '_')}.output.json at {path or '<root>'}: "
            f"{getattr(exc, 'message', str(exc))}"
        ) from exc
