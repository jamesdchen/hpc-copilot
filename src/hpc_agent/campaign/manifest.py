"""Campaign manifest — an audit record at ``<campaign_dir>/manifest.json``.

The framework only stores fields it can independently act on (``budget``
keys mirror :func:`campaign_budget`'s caps; ``stop_criteria`` keys mirror
:func:`campaign_converged`'s args) plus opaque context (``goal``,
``strategy.name``, ``strategy.params``). Never required by primitives —
purely descriptive. The agent writes the manifest once at campaign
creation; the framework only reads it for diagnostic display.

Field-mirror discipline keeps the manifest experiment-agnostic: adding
a new field requires landing the corresponding primitive arg first, so
the manifest can never describe semantics the framework can't evaluate.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hpc_agent._internal.io import atomic_locked_update
from hpc_agent._internal.time import utcnow_iso
from hpc_agent.campaign.dirs import campaign_dir

__all__ = [
    "MANIFEST_FILENAME",
    "MANIFEST_SCHEMA_VERSION",
    "manifest_path",
    "manifest_schema",
    "read_manifest",
    "validate_manifest",
    "write_manifest",
]

MANIFEST_SCHEMA_VERSION: int = 1
MANIFEST_FILENAME: str = "manifest.json"

_SCHEMA_PATH: Path = Path(__file__).resolve().parent.parent / "schemas" / "campaign_manifest.json"


def manifest_schema() -> dict[str, Any]:
    """Load and return the manifest JSON Schema as a dict."""
    data: dict[str, Any] = json.loads(_SCHEMA_PATH.read_text())
    return data


def manifest_path(experiment_dir: Path | str, campaign_id: str) -> Path:
    """Return ``<experiment_dir>/.hpc/campaigns/<campaign_id>/manifest.json``.

    Creates the parent directory idempotently via :func:`campaign_dir`.
    """
    return campaign_dir(experiment_dir, campaign_id) / MANIFEST_FILENAME


def validate_manifest(data: dict[str, Any]) -> None:
    """Validate *data* against ``schemas/campaign_manifest.json``.

    Raises
    ------
    jsonschema.ValidationError
        On any schema violation.
    """
    from hpc_agent._internal.schema import validate as _validate

    _validate(data, manifest_schema())


def write_manifest(
    experiment_dir: Path | str,
    *,
    campaign_id: str,
    goal: str = "",
    budget: dict[str, Any] | None = None,
    stop_criteria: dict[str, Any] | None = None,
    strategy: dict[str, Any] | None = None,
    created_at: str | None = None,
) -> Path:
    """Write the manifest atomically and return its path.

    All sections are optional — the framework will validate whatever
    subset the caller supplies. Pass ``strategy={"name": "...", "params": {...}}``
    to record strategy choice + opaque params (round-tripped untouched).
    """
    payload: dict[str, Any] = {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "campaign_id": campaign_id,
        "created_at": created_at or utcnow_iso(),
    }
    # Preserve goal="" (empty string is a valid explicit value) — only
    # drop None to keep the manifest tidy.
    if goal is not None:
        payload["goal"] = goal
    if budget is not None:
        payload["budget"] = budget
    if stop_criteria is not None:
        payload["stop_criteria"] = stop_criteria
    if strategy is not None:
        payload["strategy"] = strategy
    validate_manifest(payload)

    target = manifest_path(experiment_dir, campaign_id)
    # Route through atomic_locked_update so concurrent campaign_init
    # calls serialize on the same flock that advance_cursor uses; the
    # mutator just returns the new payload (read state is ignored).
    atomic_locked_update(target, lambda _existing: payload)
    return target


def read_manifest(experiment_dir: Path | str, campaign_id: str) -> dict[str, Any] | None:
    """Read and validate the manifest. Returns ``None`` if absent.

    Raises :class:`jsonschema.ValidationError` if the on-disk JSON
    violates the schema — corruption surfaces loudly rather than
    silently mis-typing fields downstream.
    """
    path = manifest_path(experiment_dir, campaign_id)
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    data: dict[str, Any] = json.loads(text)
    validate_manifest(data)
    return data
