"""Campaign manifest — an audit record at ``<campaign_dir>/manifest.json``.

The framework only stores fields it can independently act on (``budget``
keys mirror :func:`campaign_budget`'s caps; ``stop_criteria`` keys mirror
:func:`campaign_converged`'s args; ``anomaly_policy`` mirrors the loud-fail
controls :func:`campaign_advance` enforces) plus opaque context (``goal``,
``strategy.name``, ``strategy.params``) and the ``greenlit`` / ``greenlit_at``
provenance marker (design §4: the spec is greenlit once at campaign start — a
data marker, NOT an execution gate). Never required by primitives — purely
descriptive. The agent writes the manifest once at campaign creation; the
framework only reads it (and the one-shot :func:`mark_greenlit` stamp) —
never mutates the spec mid-campaign.

Field-mirror discipline keeps the manifest experiment-agnostic: adding
a new field requires landing the corresponding primitive arg first, so
the manifest can never describe semantics the framework can't evaluate.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hpc_agent.infra.io import atomic_locked_update
from hpc_agent.infra.time import utcnow_iso
from hpc_agent.meta.campaign.dirs import campaign_dir

__all__ = [
    "MANIFEST_FILENAME",
    "MANIFEST_SCHEMA_VERSION",
    "manifest_path",
    "manifest_schema",
    "mark_greenlit",
    "read_manifest",
    "validate_manifest",
    "write_manifest",
]

MANIFEST_SCHEMA_VERSION: int = 1
MANIFEST_FILENAME: str = "manifest.json"

_SCHEMA_PATH: Path = (
    Path(__file__).resolve().parent.parent.parent / "schemas" / "campaign_manifest.json"
)


def manifest_schema() -> dict[str, Any]:
    """Load and return the manifest JSON Schema as a dict."""
    data: dict[str, Any] = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
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
    from hpc_agent._kernel.contract.schema import validate as _validate

    _validate(data, manifest_schema())


def write_manifest(
    experiment_dir: Path | str,
    *,
    campaign_id: str,
    goal: str = "",
    budget: dict[str, Any] | None = None,
    stop_criteria: dict[str, Any] | None = None,
    strategy: dict[str, Any] | None = None,
    anomaly_policy: dict[str, Any] | None = None,
    async_refill: bool = False,
    max_in_flight: int | None = None,
    greenlit: bool = False,
    greenlit_at: str | None = None,
    created_at: str | None = None,
) -> Path:
    """Write the manifest atomically and return its path.

    All sections are optional — the framework will validate whatever
    subset the caller supplies. Pass ``strategy={"name": "...", "params": {...}}``
    to record strategy choice + opaque params (round-tripped untouched).

    ``anomaly_policy`` records the greenlit spec's anomaly-handling block
    (``on_anomaly`` / ``resubmit_cap`` / ``circuit_breaker_failures``), read by
    :func:`campaign_advance`.

    ``async_refill`` / ``max_in_flight`` opt the campaign into
    continuous-async refill (#362). ``greenlit`` / ``greenlit_at`` carry the
    greenlight provenance marker (design §4). All four are written ONLY when
    set — a default (``False`` / ``None``) leaves them out of the JSON so a
    synchronous, non-greenlit campaign's manifest stays byte-identical to
    today's. Post-creation, prefer :func:`mark_greenlit` to stamp the marker
    onto an existing manifest.
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
    if anomaly_policy is not None:
        payload["anomaly_policy"] = anomaly_policy
    # Only emit the async-refill opt-in when actually enabled, so a default
    # synchronous campaign's manifest is unchanged (default-off byte-identity).
    if async_refill:
        payload["async_refill"] = True
    if max_in_flight is not None:
        payload["max_in_flight"] = max_in_flight
    # Same default-off byte-identity for the greenlight marker: a non-greenlit
    # manifest carries neither key.
    if greenlit:
        payload["greenlit"] = True
    if greenlit_at is not None:
        payload["greenlit_at"] = greenlit_at
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
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    data: dict[str, Any] = json.loads(text)
    validate_manifest(data)
    return data


def mark_greenlit(
    experiment_dir: Path | str,
    *,
    campaign_id: str,
    at: str | None = None,
) -> dict[str, Any]:
    """Stamp the greenlight provenance marker onto an existing manifest.

    Sets ``greenlit=True`` + ``greenlit_at`` (ISO-8601 UTC) on the campaign's
    manifest atomically — through the SAME flock :func:`write_manifest` uses —
    and returns the updated document. Re-stamping refreshes ``greenlit_at`` but
    the marker stays ``True``.

    This is a DATA marker only (design §4: the spec is "drafted and greenlit
    once, at campaign start"). It is NOT an execution gate — no primitive
    blocks on it; it records that the greenlight happened.

    Parameters
    ----------
    at:
        ISO-8601 UTC timestamp to record; defaults to :func:`utcnow_iso`.

    Raises
    ------
    FileNotFoundError
        If the campaign has no manifest yet — the marker rides the spec, so
        the spec must exist first (write it via :func:`write_manifest` /
        ``campaign-init`` before greenlighting).
    """
    stamped_at = at or utcnow_iso()
    target = manifest_path(experiment_dir, campaign_id)

    def _stamp(existing: dict[str, Any] | None) -> dict[str, Any]:
        if existing is None:
            raise FileNotFoundError(
                f"no manifest to greenlight for campaign {campaign_id!r} at {target}"
            )
        updated = dict(existing)
        updated["greenlit"] = True
        updated["greenlit_at"] = stamped_at
        validate_manifest(updated)
        return updated

    return atomic_locked_update(target, _stamp)
