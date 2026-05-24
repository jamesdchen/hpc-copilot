"""Round-trip tests for the Pydantic-as-authoring-SoT migration.

Every model in ``scripts/build_schemas.py:SCHEMA_REGISTRY`` should
satisfy two invariants:

1. **Emitted-JSON parity** — the JSON schema that
   ``model_json_schema()`` produces today matches the file checked
   in at ``hpc_agent/schemas/<name>``. (Already enforced by the
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
from annotated_types import Ge, Gt, Le, Lt, MinLen
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMAS_DIR = REPO_ROOT / "src" / "hpc_agent" / "schemas"
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build_schemas.py"


def _load_registry() -> list[tuple[Any, str, Path]]:
    """Import ``scripts/build_schemas.py:SCHEMA_REGISTRY`` without running ``main()``.

    Entries are ``(model_or_adapter, output_filename, schemas_dir)`` —
    the ``schemas_dir`` is per-entry because the script discovers across
    multiple authoring packages (core ``hpc_agent._wire`` + pro plugin
    ``hpc_agent_pro._schema_models``) into different output directories.
    """
    spec = importlib.util.spec_from_file_location("_build_schemas_for_test", BUILD_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return list(module.SCHEMA_REGISTRY)


REGISTRY = _load_registry()


@pytest.mark.parametrize(
    "src,fname,schemas_dir",
    REGISTRY,
    ids=[fname for _, fname, _ in REGISTRY],
)
def test_emitted_schema_matches_checked_in(src: Any, fname: str, schemas_dir: Path) -> None:
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
    on_disk = (schemas_dir / fname).read_text(encoding="utf-8")
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


def _extract_max_int(metadata: list[Any], default: int) -> int:
    """Pull ``Le``/``Lt`` from *metadata* into a max bound, defaulting
    to *default* when no upper-bound constraint is declared."""
    hi: int | None = None
    for m in metadata:
        if isinstance(m, Le):
            hi = int(m.le) if hi is None else min(hi, int(m.le))
        elif isinstance(m, Lt):
            cap = int(m.lt) - 1
            hi = cap if hi is None else min(hi, cap)
    return default if hi is None else hi


def _extract_min_float(metadata: list[Any]) -> float:
    """Pull ``Ge``/``Gt`` into a float min bound. Unlike the int variant,
    ``Gt(0.0)`` becomes ``nextafter(0.0, +inf)`` rather than ``int(0.0)+1``
    so float fields like ``Field(gt=0.0, lt=1.0)`` synthesize correctly."""
    import math

    lo = 0.0
    for m in metadata:
        if isinstance(m, Ge):
            lo = max(lo, float(m.ge))
        elif isinstance(m, Gt):
            lo = max(lo, math.nextafter(float(m.gt), math.inf))
    return lo


def _extract_max_float(metadata: list[Any], default: float) -> float:
    """Pull ``Le``/``Lt`` into a float max bound. ``Lt(1.0)`` becomes
    ``nextafter(1.0, -inf)`` rather than ``int(1.0)-1``."""
    import math

    hi: float | None = None
    for m in metadata:
        if isinstance(m, Le):
            hi = float(m.le) if hi is None else min(hi, float(m.le))
        elif isinstance(m, Lt):
            cap = math.nextafter(float(m.lt), -math.inf)
            hi = cap if hi is None else min(hi, cap)
    return default if hi is None else hi


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
            return _extract_min_float(metadata)

    raise TypeError(f"synthesizer cannot handle annotation {annotation!r}")


# Override map for models whose cross-field validators require optional
# fields the per-field synthesizer would otherwise skip. Keyed by model
# qualname; values are merged into the synthesized kwargs.
_CROSS_FIELD_OVERRIDES: dict[str, dict[str, Any]] = {
    "UpdateRunConstraintsSpec": {"add_features": ["a"]},
    # _Provenance enforces ``session_sha`` when ``kind=='agent'``. The
    # generic synthesizer picks kind='agent' (first Literal value) but
    # leaves the conditionally-required session_sha unset; supply it so
    # the kind-conditional ``model_validator`` passes. The synthesizer
    # builds nested required models by name lookup, so the override
    # belongs on ``_Provenance`` itself, not on the enclosing
    # ``InterviewSpec``.
    "_Provenance": {"session_sha": "abc12345"},
}


def _synthesize_minimal(model: type[BaseModel]) -> BaseModel:
    """Build a minimal valid instance by walking required fields."""
    kwargs: dict[str, Any] = {}
    for name, info in model.model_fields.items():
        if not info.is_required():
            continue
        kwargs[name] = _resolve(info.annotation, list(info.metadata))
    kwargs.update(_CROSS_FIELD_OVERRIDES.get(model.__name__, {}))
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
    "src,fname,schemas_dir",
    [(s, f, d) for s, f, d in REGISTRY if isinstance(s, type) and issubclass(s, BaseModel)],
    ids=[f for s, f, _ in REGISTRY if isinstance(s, type) and issubclass(s, BaseModel)],
)
def test_minimal_instance_validates_against_emitted_schema(
    src: Any, fname: str, schemas_dir: Path
) -> None:
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
    schema = json.loads((schemas_dir / fname).read_text(encoding="utf-8"))
    payload = instance.model_dump(mode="json")
    jsonschema.Draft202012Validator(schema).validate(payload)


def test_typeadapter_emits_self_consistent_schemas() -> None:
    """TypeAdapters (root-array / root-union) emit schemas the runtime accepts.

    Concrete fixtures: an empty ``stages`` array fails (minItems:1)
    but a 1-stage array passes; a discriminated envelope union
    accepts a minimal success envelope.
    """
    from hpc_agent._wire.fixtures.envelope import EnvelopeAdapter, SuccessEnvelope
    from hpc_agent._wire.fixtures.stages import StagesAdapter

    # stages: 1-element list passes
    one_stage = StagesAdapter.dump_python([{"name": "fit", "run": "python fit.py"}])
    schema = json.loads((SCHEMAS_DIR / "stages.input.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(one_stage)

    # envelope: success variant validates
    success = SuccessEnvelope(ok=True, idempotent=True, data={})
    schema = json.loads((SCHEMAS_DIR / "envelope.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(EnvelopeAdapter.dump_python(success))


# ---------------------------------------------------------------------------
# Hypothesis-driven fuzz: generate diverse valid instances per model and
# verify each model_dump validates against the emitted schema.
#
# The deterministic synthesizer above pins one minimal-instance round-trip
# per model. This goes wider: hypothesis generates instances across the
# whole constraint surface (varying string lengths within pattern, varying
# optional-field presence, edge values at numeric ge/le bounds, etc.).
# Surfaces serialization edge cases a single instance can't probe.
#
# Marked @pytest.mark.slow — the deterministic synthesizer covers the
# default tier; this is the wide-net check that runs in CI.
# ---------------------------------------------------------------------------


_OMIT = object()  # sentinel for "don't pass this kwarg" in builds


def _strategy_for(annotation: Any, metadata: list[Any]) -> st.SearchStrategy:
    """Build a hypothesis strategy that produces values valid for
    *annotation* + its constraints. Mirrors ``_resolve`` but returns
    a strategy instead of a single value."""
    if annotation is Any:
        return st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=8))

    # ``Optional[T]`` expands to ``Union[T, None]`` whose args include
    # ``type(None)`` (alias ``NoneType``). Map directly to ``st.none()``.
    if annotation is type(None):
        return st.none()

    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin is Union or origin is _types_mod.UnionType:
        # Optional[T] with field-level constraints (e.g.
        # ``Optional[int] = Field(ge=1)``) stores the constraint in the
        # field's metadata, not the inner type's. Propagate metadata to
        # each non-None arm so the strategy respects ``ge=1`` etc.
        return st.one_of(*[_strategy_for(a, metadata) for a in args])

    if origin is Literal:
        return st.sampled_from(args)

    if origin is list:
        item_t = args[0] if args else str
        min_len = _extract_min_len(metadata)
        return st.lists(_strategy_for(item_t, []), min_size=min_len, max_size=min_len + 2)

    if origin is dict:
        key_t, val_t = (args + (str, str))[:2]
        min_len = _extract_min_len(metadata)
        return st.dictionaries(
            _strategy_for(key_t, []),
            _strategy_for(val_t, []),
            min_size=min_len,
            max_size=min_len + 2,
        )

    if origin is tuple:
        return st.tuples(*[_strategy_for(a, []) for a in args])

    if hasattr(annotation, "__metadata__"):
        return _strategy_for(annotation.__origin__, list(annotation.__metadata__) + metadata)

    if isinstance(annotation, type):
        if issubclass(annotation, Enum):
            return st.sampled_from(list(annotation))
        if issubclass(annotation, BaseModel):
            return _strategy_for_model(annotation)
        if annotation is bool:
            return st.booleans()
        if annotation is int:
            lo = _extract_min_int(metadata)
            hi = _extract_max_int(metadata, default=lo + 100)
            return st.integers(min_value=lo, max_value=hi)
        if annotation is float:
            lo = _extract_min_float(metadata)
            hi = _extract_max_float(metadata, default=lo + 100.0)
            return st.floats(min_value=lo, max_value=hi, allow_nan=False, allow_infinity=False)
        if annotation is str:
            pattern = None
            for m in metadata:
                p = getattr(m, "pattern", None)
                if p is not None:
                    pattern = p
                    break
            min_len = _extract_min_len(metadata)
            if pattern is not None:
                return st.from_regex(pattern, fullmatch=True).filter(lambda s: len(s) >= min_len)
            return st.text(min_size=min_len, max_size=max(min_len, 8))

    raise TypeError(f"hypothesis strategy builder cannot handle {annotation!r}")


def _strategy_for_model(model: type[BaseModel]) -> st.SearchStrategy:
    """Strategy that produces valid instances of *model*. Required fields
    always set, optional fields randomly omitted to exercise default
    paths.

    Field-level validators (e.g. uniqueness checks) reject some
    type-correct inputs; ``_build`` returns ``None`` for those and the
    surrounding ``filter`` discards them. Hypothesis sees this as a
    rejection, similar to ``assume()``.
    """
    from pydantic import ValidationError

    field_strategies: dict[str, st.SearchStrategy] = {}
    for name, info in model.model_fields.items():
        s = _strategy_for(info.annotation, list(info.metadata))
        if info.is_required():
            field_strategies[name] = s
        else:
            field_strategies[name] = st.one_of(st.just(_OMIT), s)

    def _build(**kwargs: Any) -> BaseModel | None:
        try:
            return model(**{k: v for k, v in kwargs.items() if v is not _OMIT})
        except ValidationError:
            return None

    return st.builds(_build, **field_strategies).filter(lambda x: x is not None)


@pytest.mark.slow
@pytest.mark.parametrize(
    "src,fname,schemas_dir",
    [(s, f, d) for s, f, d in REGISTRY if isinstance(s, type) and issubclass(s, BaseModel)],
    ids=[f for s, f, _ in REGISTRY if isinstance(s, type) and issubclass(s, BaseModel)],
)
def test_fuzz_instances_validate_against_emitted_schema(
    src: Any, fname: str, schemas_dir: Path
) -> None:
    """For every model in the registry, generate diverse valid instances
    and verify each ``model_dump`` validates against the emitted JSON
    schema. Surfaces the "Pydantic emits a schema it can't validate its
    own output against" bug class across the constraint surface, not
    just the single minimal-instance corner."""
    try:
        strategy = _strategy_for_model(src)
    except TypeError as exc:
        pytest.skip(f"{fname}: strategy builder doesn't handle a field type — {exc}")

    schema = json.loads((schemas_dir / fname).read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)

    @given(strategy)
    @settings(
        max_examples=15,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
    )
    def _check(instance: BaseModel) -> None:
        validator.validate(instance.model_dump(mode="json"))

    _check()
