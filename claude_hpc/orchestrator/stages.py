"""Multi-stage DAG loader for ``.hpc/stages.py``.

Multi-stage DAGs are expressed in Python alongside ``.hpc/tasks.py``,
mirroring the same convention:

    def stages() -> list[dict]:
        return [
            {"name": "ingest",  "run": "..."},
            {"name": "fit",     "run": "...", "depends_on": "ingest"},
            {"name": "predict", "run": "...", "depends_on": "fit"},
        ]

The dict schema is published at ``schemas/stages.input.json``; agents
generate dict literals and ``load_stages`` validates them at load time.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import jsonschema

if TYPE_CHECKING:
    from types import ModuleType

__all__ = [
    "STAGES_FILENAME",
    "load_stages",
    "load_stages_module",
    "stages_path",
    "stages_schema",
    "validate_stages",
]

STAGES_FILENAME: str = "stages.py"

# JSON Schema lives next to submit.input.json / status.output.json so the
# package-data glob in pyproject.toml ships it automatically.
_SCHEMA_PATH: Path = Path(__file__).resolve().parent.parent / "schemas" / "stages.input.json"


def stages_path(experiment_dir: Path) -> Path:
    """Return ``experiment_dir/.hpc/stages.py`` (does not create the file)."""
    return Path(experiment_dir) / ".hpc" / STAGES_FILENAME


def stages_schema() -> dict[str, Any]:
    """Load and return the stages JSON Schema as a dict."""
    data: dict[str, Any] = json.loads(_SCHEMA_PATH.read_text())
    return data


def validate_stages(stages: list[dict[str, Any]]) -> None:
    """Validate ``stages`` against ``schemas/stages.input.json``.

    Beyond schema-level checks, also verify:
      * stage names are unique within the list
      * every ``depends_on`` reference resolves to a stage name in the list

    Raises
    ------
    jsonschema.ValidationError
        If the schema check fails.
    ValueError
        If names collide or ``depends_on`` references an unknown stage.
    """
    jsonschema.validate(instance=stages, schema=stages_schema())
    names = [s["name"] for s in stages]
    seen: set[str] = set()
    duplicates: list[str] = []
    for n in names:
        if n in seen:
            duplicates.append(n)
        seen.add(n)
    if duplicates:
        raise ValueError(f"duplicate stage names: {sorted(set(duplicates))}")
    name_set = set(names)
    for stage in stages:
        deps = stage.get("depends_on")
        if deps is None:
            continue
        dep_list = [deps] if isinstance(deps, str) else list(deps)
        unknown = [d for d in dep_list if d not in name_set]
        if unknown:
            raise ValueError(f"stage {stage['name']!r} depends_on unknown stage(s): {unknown}")


def load_stages_module(stages_py_path: Path) -> ModuleType:
    """Import a user's ``stages.py`` from an arbitrary path via importlib.

    The returned module must expose a callable ``stages()`` returning a
    list of dicts. Callers should treat any ``AttributeError``,
    ``TypeError``, or ``ImportError`` from the user's code as a
    submit-time error worth surfacing, not a framework bug.
    """
    path = Path(stages_py_path)
    if not path.is_file():
        raise FileNotFoundError(f"stages.py not found: {path}")
    spec = importlib.util.spec_from_file_location("hpc_user_stages", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load stages.py from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "stages") or not callable(module.stages):
        raise AttributeError(
            f"{path} must define a stages() callable returning list[dict] — "
            "see claude_hpc/schemas/stages.input.json"
        )
    return module


def load_stages(experiment_dir: Path) -> list[dict[str, Any]]:
    """Load ``.hpc/stages.py``, call ``stages()``, validate, and return.

    Convenience wrapper combining ``load_stages_module`` and
    ``validate_stages``. Raises ``FileNotFoundError`` if the file is
    absent so callers can distinguish "no DAG configured" from
    "malformed DAG."
    """
    module = load_stages_module(stages_path(experiment_dir))
    result = module.stages()
    if not isinstance(result, list):
        raise TypeError(f"stages() must return list[dict], got {type(result).__name__}")
    validate_stages(result)
    return result
