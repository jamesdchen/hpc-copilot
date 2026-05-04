"""Cross-validation: every inline copy of a canonical envelope.json $def must
stay byte-equivalent to the canonical version.

Rationale: the agent_cli's ``jsonschema.validate`` path does not register a
``RefResolver``, so cross-file ``$ref`` (e.g. ``envelope.json#/$defs/scheduler``)
fails to resolve. Consumer schemas must therefore inline these definitions
verbatim. To keep them from drifting out of step on additive changes we walk
every schema file at test time and compare each occurrence of a
canonically-named property (``run_id``, ``combined_waves``, ``failed_waves``,
``lifecycle_state``, ``scheduler``, ``gpu_type``, ``error_code``) against the
canonical ``$defs`` entry in ``envelope.json``.

The comparison ignores documentation-only keys (``description``, ``title``,
``$comment``) — those don't affect validation behaviour. A consumer property
matches when its validation-affecting shape equals at least one of the
canonical aliases listed for that name (e.g. ``lifecycle_state`` may match the
terminal, observable, or observable+timeout flavour depending on the caller).

Known intentional drifts are listed in ``KNOWN_LOOSE`` — consumers that have
deliberately relaxed the canonical shape for a documented wire-contract
reason. Tightening any of these would require coordinated wire-contract work,
not a $defs-dedup pass.
"""

from __future__ import annotations

import json
from importlib.resources import files as _resource_files
from typing import Any

# ---------------------------------------------------------------------------
# canonical $defs aliases — name -> tuple of $defs keys that are acceptable matches
# ---------------------------------------------------------------------------

CANONICAL_ALIASES: dict[str, tuple[str, ...]] = {
    "run_id": ("run_id", "run_id_strict"),
    "combined_waves": ("combined_waves",),
    "failed_waves": ("failed_waves",),
    "lifecycle_state": (
        "lifecycle_state_terminal",
        "lifecycle_state_observable",
        "lifecycle_state_observable_with_timeout",
    ),
    "scheduler": ("scheduler",),
    "gpu_type": ("gpu_type",),
    "error_code": ("error_code",),
}

# Known intentional drifts: (file, dotted_path) -> reason. Adding to this list
# is a deliberate wire-contract decision, not a dedup-schemas action.
KNOWN_LOOSE: dict[tuple[str, str], str] = {
    (
        "validate.output.json",
        "properties.scheduler",
    ): (
        "validate output predates the scheduler enum and historically accepted "
        "any string; tightening risks breaking fixtures with non-{sge,slurm} drivers."
    ),
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

DOC_KEYS = {"description", "title", "$comment"}


def _normalize(node: Any) -> Any:
    """Strip documentation-only keys recursively so the comparison only looks
    at validation-affecting shape."""
    if isinstance(node, dict):
        return {k: _normalize(v) for k, v in node.items() if k not in DOC_KEYS}
    if isinstance(node, list):
        return [_normalize(x) for x in node]
    return node


def _walk_props(node: Any, path: tuple[str | int, ...] = ()):  # noqa: ANN401
    """Yield (property_name, dotted_path, definition) for every entry inside
    ``properties`` or ``$defs`` blocks anywhere in the schema tree."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k in {"properties", "$defs"} and isinstance(v, dict):
                for prop_name, prop_def in v.items():
                    yield (prop_name, path + (k, prop_name), prop_def)
                    yield from _walk_props(prop_def, path + (k, prop_name))
            else:
                yield from _walk_props(v, path + (k,))
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from _walk_props(item, path + (i,))


def _load_schema(name: str) -> dict[str, Any]:
    return json.loads((_resource_files("claude_hpc.schemas") / name).read_text())


def _list_schema_files() -> list[str]:
    return [
        p.name
        for p in (_resource_files("claude_hpc.schemas")).iterdir()  # type: ignore[attr-defined]
        if p.name.endswith(".json")
    ]


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_envelope_defines_all_canonical_aliases() -> None:
    """envelope.json:$defs must contain every alias referenced by callers."""
    envelope = _load_schema("envelope.json")
    defs = envelope["$defs"]
    for prop_name, aliases in CANONICAL_ALIASES.items():
        for alias in aliases:
            assert alias in defs, (
                f"envelope.json:$defs missing canonical alias '{alias}' "
                f"(expected for property '{prop_name}')"
            )


def test_inline_copies_match_canonical_defs() -> None:
    """Every consumer schema's inline copy of a canonical-named property must
    have the same validation-affecting shape as one of the canonical aliases."""
    envelope = _load_schema("envelope.json")
    defs = envelope["$defs"]

    canonical_shapes = {
        alias: json.dumps(_normalize(defs[alias]), sort_keys=True)
        for aliases in CANONICAL_ALIASES.values()
        for alias in aliases
    }

    drifts: list[str] = []
    for fname in sorted(_list_schema_files()):
        if fname == "envelope.json":
            continue
        data = _load_schema(fname)
        for prop_name, p, defn in _walk_props(data):
            if prop_name not in CANONICAL_ALIASES:
                continue
            dotted = ".".join(str(x) for x in p)
            if (fname, dotted) in KNOWN_LOOSE:
                continue
            inline_shape = json.dumps(_normalize(defn), sort_keys=True)
            aliases = CANONICAL_ALIASES[prop_name]
            if not any(canonical_shapes[a] == inline_shape for a in aliases):
                expected = " | ".join(f"{a}={canonical_shapes[a]}" for a in aliases)
                drifts.append(
                    f"{fname} @ {dotted}: inline shape {inline_shape!r} "
                    f"matches none of [{expected}]"
                )

    assert not drifts, (
        "Inline schema copies have drifted from canonical envelope.json:$defs:\n  "
        + "\n  ".join(drifts)
    )


def test_envelope_oneof_error_code_matches_canonical_defs() -> None:
    """The error_code shape inside envelope.json's failure branch must equal
    the canonical envelope.json:$defs/error_code."""
    envelope = _load_schema("envelope.json")
    inline = envelope["oneOf"][1]["properties"]["error_code"]
    canonical = envelope["$defs"]["error_code"]
    assert _normalize(inline) == _normalize(canonical), (
        "envelope.json oneOf[1].properties.error_code drifted from envelope.json:$defs/error_code"
    )


def test_known_loose_entries_still_loose() -> None:
    """Sanity: every entry in KNOWN_LOOSE must still actually be looser than
    the canonical shape — otherwise the entry is stale and should be removed
    so the inline copy joins the canonical track."""
    envelope = _load_schema("envelope.json")
    defs = envelope["$defs"]
    canonical_shapes = {
        alias: json.dumps(_normalize(defs[alias]), sort_keys=True)
        for aliases in CANONICAL_ALIASES.values()
        for alias in aliases
    }

    stale: list[str] = []
    for (fname, dotted), _reason in KNOWN_LOOSE.items():
        data = _load_schema(fname)
        # Walk to the dotted path
        node: Any = data
        for token in dotted.split("."):
            node = node[int(token)] if isinstance(node, list) else node[token]
        inline_shape = json.dumps(_normalize(node), sort_keys=True)
        # Find the property name (last dotted segment)
        prop_name = dotted.split(".")[-1]
        aliases = CANONICAL_ALIASES.get(prop_name, ())
        if any(canonical_shapes[a] == inline_shape for a in aliases):
            stale.append(
                f"{fname} @ {dotted}: KNOWN_LOOSE entry no longer drifts; "
                f"remove it from the allowlist so the test enforces tightness."
            )
    assert not stale, "\n".join(stale)
