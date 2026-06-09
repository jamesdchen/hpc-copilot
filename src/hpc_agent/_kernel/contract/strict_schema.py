"""Transform a Pydantic-generated JSON Schema into a strict-decode copy.

Strict structured-output mode (OpenAI's ``json_schema`` strict, self-hosted
vLLM guided decoding, and Codex's ``--output-schema`` file) requires, on
every object node: ``additionalProperties: false`` and ``required`` listing
*all* of ``properties``. Pydantic emits optional/defaulted fields that aren't
in ``required``, which strict mode rejects.

This is the single canonical strictifier. Two consumers share it so the
transform never forks:

* :mod:`hpc_agent._kernel.lifecycle.chat_models.openai_compat` — the runtime
  ``ChatModel`` accelerator, which strictifies the offered schema per call.
* ``scripts/build_schemas.py`` — generates the checked-in
  ``worker.strict.output.json`` from ``WorkerReport`` so the spawned Codex
  worker (``--output-schema``) binds an API-strict file (not the lenient
  floor schema).

**Invariant:** a strict-decoded output is always a *superset-constrained*
case of the lenient validate. Forcing every property required and forbidding
extras only narrows what the server may emit; anything that passes the strict
constraint trivially passes the original lenient Pydantic model. So the
accelerator and the parse-validate-repair floor never conflict — the floor can
only ever accept *more* than the strict decode produced.
"""

from __future__ import annotations

from typing import Any

from hpc_agent import errors

# A small, conservative set: strict structured-output mode rejects these
# string formats. Dropping them loses only a producer-side hint the floor
# does not need (Pydantic re-validates the value). Kept minimal — extend only
# on a concrete observed rejection.
_UNSUPPORTED_FORMATS = frozenset({"email", "uri", "uuid", "hostname", "ipv4", "ipv6"})

# Schema keys whose value is a name → subschema mapping (recurse into each
# value, not the mapping itself).
_SUBSCHEMA_MAPS = frozenset({"properties", "$defs", "definitions"})


def to_strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a strict-decode copy of a Pydantic-generated JSON Schema.

    Applied ONLY to the decode-constraint copy (the floor still validates
    with the original lenient model). Recurses through ``properties`` values,
    ``items``, ``$defs`` (and ``definitions``), and the ``anyOf`` / ``oneOf`` /
    ``allOf`` branch lists. Internal ``$ref`` / ``$defs`` are preserved (not
    inlined) *below* the root: OpenAI strict and vLLM guided decoding both
    accept internal refs. ``format`` keywords known to be unsupported by strict
    mode are dropped (kept minimal).

    **Object root guarantee.** Strict mode also requires the *root* to be an
    object. A normal ``BaseModel`` already emits a flat object root, but a
    ``RootModel`` emits a bare ``{"$ref": ...}`` root (wrapping a model) or a
    ``type: array`` / scalar root (wrapping a non-object).
    :func:`_promote_object_root` inlines a root ``$ref`` so the former just
    works, and raises a clear error on the latter rather than emit a payload
    the API rejects with an opaque 400.
    """
    return _promote_object_root(_strict_node(schema))


def _resolve_local_ref(ref: str, defs: dict[str, Any]) -> Any:
    """Return the ``#/$defs/<name>`` subschema from *defs*, or ``None``."""
    prefix = "#/$defs/"
    return defs.get(ref[len(prefix) :]) if ref.startswith(prefix) else None


def _promote_object_root(schema: dict[str, Any]) -> dict[str, Any]:
    """Guarantee a strict-valid *object* root, inlining a root ``$ref``.

    Pydantic emits a bare ``{"$ref": "#/$defs/X"}`` root for a ``RootModel``
    wrapping a model (and an ``allOf: [{"$ref": ...}]`` wrapper in some
    shapes). Resolve that ref against the already-strictified ``$defs`` and
    promote the referenced object to the root, carrying ``$defs`` forward so
    inner refs still resolve. A genuinely non-object root (a ``RootModel`` over
    a list / scalar / union) cannot be expressed in strict mode, so raise a
    clear :class:`~hpc_agent.errors.SpecInvalid` naming the ``json_object``
    fallback rather than send a payload the endpoint 400s on.
    """
    root = schema
    defs = root.get("$defs")
    ref = root.get("$ref")
    if ref is None:
        all_of = root.get("allOf")
        if (
            isinstance(all_of, list)
            and len(all_of) == 1
            and isinstance(all_of[0], dict)
            and set(all_of[0]) == {"$ref"}
        ):
            ref = all_of[0]["$ref"]
    if isinstance(ref, str) and isinstance(defs, dict):
        target = _resolve_local_ref(ref, defs)
        if isinstance(target, dict):
            root = {**target, "$defs": defs}
            if "title" in schema:
                root.setdefault("title", schema["title"])
    if root.get("type") != "object" or "properties" not in root:
        raise errors.SpecInvalid(
            "strict json_schema decode requires an object-rooted schema, but the "
            "target model produced a non-object root (e.g. a RootModel wrapping a "
            "list / scalar / union). Set HPC_AGENT_MODEL_RESPONSE_FORMAT=json_object "
            "to use JSON-mode + the parse-validate-repair floor for this schema, or "
            "wrap the value in a BaseModel field."
        )
    return root


def _strict_node(node: Any) -> Any:
    """Recursively strictify a JSON-Schema *node* (dict / list / scalar)."""
    if isinstance(node, list):
        return [_strict_node(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _SUBSCHEMA_MAPS and isinstance(value, dict):
            out[key] = {name: _strict_node(sub) for name, sub in value.items()}
        elif key in {"anyOf", "oneOf", "allOf"} and isinstance(value, list):
            out[key] = [_strict_node(item) for item in value]
        elif key == "items":
            out[key] = _strict_node(value)
        elif key == "format" and value in _UNSUPPORTED_FORMATS:
            continue  # drop the unsupported format hint
        else:
            out[key] = _strict_node(value)
    # On an object node, force strict's two requirements.
    if out.get("type") == "object" or "properties" in out:
        properties = out.get("properties")
        if isinstance(properties, dict):
            out["additionalProperties"] = False
            out["required"] = list(properties.keys())
    return out


__all__ = ["to_strict_schema"]
