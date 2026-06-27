"""Tests for the async-refill manifest fields (#362, plan §1.1).

``CampaignManifest`` gained two optional top-level fields — ``async_refill``
(bool, default ``False``) and ``max_in_flight`` (int|None, default ``None``).
These pin:

* round-trip through ``write_manifest`` / ``read_manifest`` / the model;
* ``extra="forbid"`` still rejects unknown keys (the schema is strict);
* absent fields default (a pre-async manifest still validates);
* **default-off byte-identity** — a synchronous campaign's manifest carries
  neither key, so existing manifests are unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from hpc_agent._wire.fixtures.campaign_manifest import CampaignManifest
from hpc_agent.meta.campaign.atoms.init import campaign_init
from hpc_agent.meta.campaign.manifest import (
    read_manifest,
    validate_manifest,
    write_manifest,
)


def test_model_round_trips_async_fields() -> None:
    """The Pydantic model accepts + round-trips both new fields."""
    m = CampaignManifest(
        manifest_schema_version=1,
        campaign_id="tune_lr",
        async_refill=True,
        max_in_flight=4,
    )
    dumped = m.model_dump(mode="json")
    assert dumped["async_refill"] is True
    assert dumped["max_in_flight"] == 4
    assert CampaignManifest.model_validate(dumped).max_in_flight == 4


def test_async_fields_default_when_absent() -> None:
    """A manifest written before async-refill existed still validates; the
    model supplies the defaults (off / unbounded)."""
    m = CampaignManifest(manifest_schema_version=1, campaign_id="legacy")
    assert m.async_refill is False
    assert m.max_in_flight is None


def test_schema_still_forbids_unknown_keys() -> None:
    """extra='forbid' / additionalProperties:false is intact — a typo'd
    field is rejected rather than silently round-tripped."""
    bad = {
        "manifest_schema_version": 1,
        "campaign_id": "c",
        "asyncrefill": True,  # typo — not a real field
    }
    with pytest.raises(jsonschema.ValidationError):
        validate_manifest(bad)


def test_max_in_flight_must_be_positive() -> None:
    """``max_in_flight`` is ge=1 — a zero/negative pool target is rejected."""
    with pytest.raises(jsonschema.ValidationError):
        validate_manifest({"manifest_schema_version": 1, "campaign_id": "c", "max_in_flight": 0})


def test_write_manifest_round_trips_async_on(tmp_path: Path) -> None:
    write_manifest(tmp_path, campaign_id="camp", async_refill=True, max_in_flight=3)
    data = read_manifest(tmp_path, "camp")
    assert data is not None
    assert data["async_refill"] is True
    assert data["max_in_flight"] == 3


def test_write_manifest_default_off_is_byte_identical(tmp_path: Path) -> None:
    """A synchronous campaign's manifest carries NEITHER async key — the
    default path's on-disk bytes are unchanged by this feature."""
    path = write_manifest(tmp_path, campaign_id="sync", goal="tune")
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "async_refill" not in raw
    assert "max_in_flight" not in raw


def test_campaign_init_plumbs_async_flags(tmp_path: Path) -> None:
    """``campaign-init --async-refill --max-in-flight K`` persists the opt-in."""
    campaign_init(
        experiment_dir=tmp_path,
        campaign_id="camp",
        async_refill=True,
        max_in_flight=4,
    )
    data = read_manifest(tmp_path, "camp")
    assert data is not None
    assert data["async_refill"] is True
    assert data["max_in_flight"] == 4
