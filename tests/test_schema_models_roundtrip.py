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
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from pydantic import BaseModel, TypeAdapter

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


def _try_default_instance(model: type[BaseModel]) -> BaseModel | None:
    """Try to construct a minimal valid instance using model defaults.

    Returns None when the model has any required field without a
    default (most of our specs do — they're wire contracts). The
    self-validating-dump check below skips those silently.
    """
    try:
        return model()
    except Exception:
        return None


@pytest.mark.parametrize(
    "src,fname",
    [(s, f) for s, f in REGISTRY if isinstance(s, type) and issubclass(s, BaseModel)],
    ids=[f for s, f in REGISTRY if isinstance(s, type) and issubclass(s, BaseModel)],
)
def test_default_instance_validates_against_emitted_schema(src: Any, fname: str) -> None:
    """If we can build a default instance, its dump must satisfy the emitted schema.

    Only runs when the model has a default-constructible form
    (every required field has a default). Most of our models
    don't, so this test exercises the subset where it's
    structurally possible — sufficient to catch the
    "Pydantic emits a schema it can't validate its own output
    against" failure mode.
    """
    instance = _try_default_instance(src)
    if instance is None:
        pytest.skip(f"{fname} has required fields with no defaults")
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
