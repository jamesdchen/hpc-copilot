"""Round-trip tests for the Pydantic-as-authoring-SoT migration.

Every model in ``scripts/build_schemas.py:SCHEMA_REGISTRY`` should
satisfy two invariants:

1. **Emitted-JSON parity** — the JSON schema that
   ``model_json_schema()`` produces today matches the file checked
   in at ``claude_hpc/schemas/<name>``. (Already enforced by the
   pre-commit ``--check`` gate, but pinning it here means a stale
   checked-in schema also fails CI without needing the gate.)

2. **Self-validating dump** — for any concrete instance Pydantic
   itself accepts, the emitted JSON must validate that instance's
   ``model_dump(mode="json")`` output. Catches the case where a
   subtle constraint combination produces a schema that doesn't
   accept its own author's output (rare in Pydantic v2 but
   possible for unusual `Annotated` overrides or custom
   serializers).

Test #2 only runs against models / adapters where we can construct
a concrete minimal instance from declared examples. Models without
a fixture are still covered by test #1 (which catches static
drift); the absence of a fixture isn't a failure.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types as _types_mod
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin

import jsonschema
import pytest
from annotated_types import Ge, Gt, MinLen
from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = REPO_ROOT / "src" / "claude_hpc" / "schemas"
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build_schemas.py"


def _load_registry() -> list[tuple[Any, str]]:
    """Import ``scripts/build_schemas.py:SCHEMA_REGISTRY`` without running ``main()``."""
    spec = importlib.util.spec_from_file_location("_build_schemas_for_test", BUILD_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return list(module.SCHEMA_REGISTRY)


REGISTRY = _load_registry()


@pytest.mark.parametrize(
    "src,fname",
    REGISTRY,
    ids=[fname for _, fname in REGISTRY],
)
def test_emitted_schema_matches_checked_in(src: Any, fname: str) -> None:
    """The Pydantic-emitted schema is byte-equal to the checked-in JSON file.

    Mirrors what ``scripts/build_schemas.py --check`` does in
    pre-commit, but as a unit test so a fresh clone running
    ``pytest`` catches drift even before pre-commit fires.
    """
    spec = importlib.util.spec_from_file_location("_build_schemas_for_test", BUILD_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = sys.modules.get(spec.name) or importlib.util.module_from_spec(spec)
    if spec.name not in sys.modules:
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

    emitted = module._emit(src, fname)
    on_disk = (SCHEMAS_DIR / fname).read_text(encoding="utf-8")
    assert emitted == on_disk, (
        f"{fname}: Pydantic emission drifted from checked-in JSON. "
        "Run scripts/build_schemas.py --write to regenerate."
    )


# ---------------------------------------------------------------------------
# Self-validating dump fixtures.
#
# For each model where we can synthesize a minimal valid instance,
# verify that its model_dump validates against the emitted schema.
# Skipping a model here is fine — test #1 still covers static drift.
# ---------------------------------------------------------------------------


# Patterns we use across shared.py have known minimal-matching strings.
# Anything else (a model adds a new pattern) raises and the test fails
# with a clear "add a fixture" error instead of silently skipping.
_PATTERN_FIXTURES: dict[str, str] = {
    r"^[A-Za-z0-9._\-]+$": "x",  # RunIdStrict, CampaignId
    r"^[^@]+@[^@]+$": "u@h",  # SshTarget
    r"^[0-9a-fA-F]{64}$": "a" * 64,  # cmd_sha (build_submit_spec)
    r"^[0-9a-f]{8,64}$": "a" * 8,  # cmd_sha (interview)
}


def _extract_min_len(metadata: list[Any]) -> int:
    for m in metadata:
        if isinstance(m, MinLen):
            return m.min_length
        ml = getattr(m, "min_length", None)
        if ml is not None:
            return ml
    return 0


def _extract_min_int(metadata: list[Any]) -> int:
    lo = 0
    for m in metadata:
        if isinstance(m, Ge):
            lo = max(lo, int(m.ge))
        elif isinstance(m, Gt):
            lo = max(lo, int(m.gt) + 1)
    return lo


def _string_for_metadata(metadata: list[Any]) -> str:
    pattern = None
    for m in metadata:
        # StringConstraints exposes ``.pattern``; Pydantic's internal
        # ``_PydanticGeneralMetadata`` (used by ``Field(pattern=...)``)
        # also exposes ``.pattern`` — duck-typing covers both.
        candidate = getattr(m, "pattern", None)
        if candidate is not None:
            pattern = candidate
            break
    base = _PATTERN_FIXTURES[pattern] if pattern is not None else "x"
    min_len = max(_extract_min_len(metadata), len(base))
    return base + "x" * (min_len - len(base))


def _resolve(annotation: Any, metadata: list[Any]) -> Any:
    """Synthesize a minimal valid value for *annotation* given its constraints."""
    # ``typing.Any`` accepts anything; ``None`` is the smallest valid payload.
    if annotation is Any:
        return None

    origin = get_origin(annotation)
    args = get_args(annotation)

    # Optional / Union with None — prefer None for minimality.
    if origin is Union or origin is _types_mod.UnionType:
        if type(None) in args:
            return None
        return _resolve(args[0], metadata)

    if origin is Literal:
        return args[0]

    if origin is list:
        item_type = args[0] if args else str
        return [_resolve(item_type, []) for _ in range(_extract_min_len(metadata))]

    if origin is dict:
        min_len = _extract_min_len(metadata)
        if min_len == 0:
            return {}
        key_t, val_t = (args + (str, str))[:2]
        return {f"k{i}": _resolve(val_t, []) for i in range(min_len)}

    if origin is tuple:
        return tuple(_resolve(a, []) for a in args)

    # Annotated[T, ...] — Pydantic flattens into field.annotation + metadata,
    # but nested annotations may still appear; recurse on the inner type.
    if hasattr(annotation, "__metadata__"):
        inner_meta = list(annotation.__metadata__) + metadata
        return _resolve(annotation.__origin__, inner_meta)

    if isinstance(annotation, type):
        if issubclass(annotation, Enum):
            return next(iter(annotation))
        if issubclass(annotation, BaseModel):
            return _synthesize_minimal(annotation)
        if annotation is bool:
            return False
        if annotation is str:
            return _string_for_metadata(metadata)
        if annotation is int:
            return _extract_min_int(metadata)
        if annotation is float:
            return float(_extract_min_int(metadata))

    raise TypeError(f"synthesizer cannot handle annotation {annotation!r}")


def _synthesize_minimal(model: type[BaseModel]) -> BaseModel:
    """Build a minimal valid instance by walking required fields."""
    kwargs: dict[str, Any] = {}
    for name, info in model.model_fields.items():
        if not info.is_required():
            continue
        kwargs[name] = _resolve(info.annotation, list(info.metadata))
    return model(**kwargs)


def _try_minimal_instance(model: type[BaseModel]) -> BaseModel | None:
    """Synthesize the minimal valid instance; return None if a constraint
    is unrepresentable (e.g. an unrecognized regex). The test failure
    message points at exactly which model needs a new fixture."""
    try:
        return _synthesize_minimal(model)
    except Exception:
        return None


@pytest.mark.parametrize(
    "src,fname",
    [(s, f) for s, f in REGISTRY if isinstance(s, type) and issubclass(s, BaseModel)],
    ids=[f for s, f in REGISTRY if isinstance(s, type) and issubclass(s, BaseModel)],
)
def test_minimal_instance_validates_against_emitted_schema(src: Any, fname: str) -> None:
    """A minimal valid instance of *src* must dump to JSON the emitted
    schema accepts. Catches the "Pydantic emits a schema it can't
    validate its own output against" failure mode for every model
    in the registry, not just the default-constructible ones."""
    instance = _try_minimal_instance(src)
    assert instance is not None, (
        f"{fname}: synthesizer could not build a minimal instance. "
        f"Add a pattern fixture to ``_PATTERN_FIXTURES`` or extend "
        f"``_resolve`` to cover the new type."
    )
    schema = json.loads((SCHEMAS_DIR / fname).read_text(encoding="utf-8"))
    payload = instance.model_dump(mode="json")
    jsonschema.Draft202012Validator(schema).validate(payload)


def test_typeadapter_emits_self_consistent_schemas() -> None:
    """TypeAdapters (root-array / root-union) emit schemas the runtime accepts.

    Concrete fixtures: an empty ``stages`` array fails (minItems:1)
    but a 1-stage array passes; a discriminated envelope union
    accepts a minimal success envelope.
    """
    from claude_hpc._schema_models.envelope import EnvelopeAdapter, SuccessEnvelope
    from claude_hpc._schema_models.stages import StagesAdapter

    # stages: 1-element list passes
    one_stage = StagesAdapter.dump_python([{"name": "fit", "run": "python fit.py"}])
    schema = json.loads((SCHEMAS_DIR / "stages.input.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(one_stage)

    # envelope: success variant validates
    success = SuccessEnvelope(ok=True, idempotent=True, data={})
    schema = json.loads((SCHEMAS_DIR / "envelope.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(EnvelopeAdapter.dump_python(success))
