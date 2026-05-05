"""Per-experiment axes config + cold-start axis picker.

Lives at ``<experiment>/.hpc/axes.yaml``, a one-line file in the common
case::

    axes_schema_version: 1
    homogeneous_axes: [window]

The agent writes this once at deploy time (during ``setup_hpc`` or
``hpc-build-executor``); the framework reads it at submit time to
decide which axis becomes the task array. Field-mirror discipline:
the framework only stores fields it can independently act on, so
:data:`homogeneous_axes` is the only signal here. Experiment-specific
reasoning about WHY an axis is homogeneous lives in the agent's chat
context for that repo.

Two-path picker:

* **Warm** — when runtime priors exist for this ``cmd_sha``, the picker
  computes coefficient-of-variation per axis and picks the lowest-CV
  one (planned; not yet implemented).
* **Cold** — when no priors exist, fall back to the first axis in
  :data:`homogeneous_axes` that appears in ``tasks.py``'s ``AXES``
  declaration. This module implements the cold path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import yaml

__all__ = [
    "AXES_FILENAME",
    "AXES_SCHEMA_VERSION",
    "axes_path",
    "axes_schema",
    "pick_array_axis",
    "read_axes",
    "validate_axes",
    "write_axes",
]

AXES_SCHEMA_VERSION: int = 1
AXES_FILENAME: str = "axes.yaml"

_SCHEMA_PATH: Path = Path(__file__).resolve().parent.parent / "schemas" / "axes.json"


def axes_schema() -> dict[str, Any]:
    """Load and return the axes JSON Schema as a dict."""
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def axes_path(experiment_dir: Path | str) -> Path:
    """Return ``<experiment_dir>/.hpc/axes.yaml`` (does not create it)."""
    return Path(experiment_dir) / ".hpc" / AXES_FILENAME


def validate_axes(data: dict[str, Any]) -> None:
    """Validate *data* against ``schemas/axes.json``.

    Raises :class:`jsonschema.ValidationError` on any schema violation.
    """
    jsonschema.validate(instance=data, schema=axes_schema())


def write_axes(
    experiment_dir: Path | str,
    *,
    homogeneous_axes: list[str] | None = None,
) -> Path:
    """Write the axes config atomically and return its path."""
    payload: dict[str, Any] = {"axes_schema_version": AXES_SCHEMA_VERSION}
    if homogeneous_axes is not None:
        payload["homogeneous_axes"] = list(homogeneous_axes)
    validate_axes(payload)
    target = axes_path(experiment_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")
    tmp.replace(target)
    return target


def read_axes(experiment_dir: Path | str) -> dict[str, Any] | None:
    """Read and validate the axes config; return ``None`` if absent.

    Raises :class:`jsonschema.ValidationError` if the on-disk file
    violates the schema (corruption surfaces loudly).
    """
    path = axes_path(experiment_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level YAML must be a mapping, got {type(data).__name__}")
    validate_axes(data)
    return data


def pick_array_axis(
    experiment_dir: Path | str,
    *,
    available_axes: list[str] | None = None,
) -> tuple[str | None, str]:
    """Cold-start axis picker — return ``(axis_name, reason)``.

    *axis_name* is the axis to promote onto the task array, or
    ``None`` when no decision can be made (the caller — typically the
    submit-flow / agent — falls back to asking the user).

    *available_axes*, if supplied, is the list of axis names from the
    user's ``tasks.py`` ``AXES`` declaration. The picker only returns
    names that appear in this list; entries in ``axes.yaml`` that don't
    match are silently skipped (a separate validator should warn about
    those at deploy time).

    This is the cold path: it reads only :data:`homogeneous_axes`. The
    warm path (lowest-CV from runtime priors) is planned and will
    supersede this; the cold-start fallback then continues to live
    here.
    """
    config = read_axes(experiment_dir)
    if config is None:
        return None, "no axes.yaml"
    homogeneous = config.get("homogeneous_axes") or []
    if not homogeneous:
        return None, "homogeneous_axes is empty"
    if available_axes is None:
        chosen = homogeneous[0]
        return chosen, f"first entry in homogeneous_axes ({chosen!r})"
    for name in homogeneous:
        if name in available_axes:
            return name, f"first homogeneous_axes entry that matches tasks.py AXES ({name!r})"
    return (
        None,
        f"no homogeneous_axes entry matches available axes {available_axes!r}",
    )
