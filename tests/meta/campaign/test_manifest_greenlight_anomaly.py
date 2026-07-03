"""Tests for the greenlight provenance marker + anomaly_policy manifest block.

``CampaignManifest`` gained three optional fields (human-amplification design
§4): the ``greenlit`` / ``greenlit_at`` provenance marker (a DATA flag, not an
execution gate) and the ``anomaly_policy`` block (``on_anomaly`` /
``resubmit_cap`` / ``circuit_breaker_failures``, mirroring controls
``campaign-advance`` enforces). These pin:

* the Pydantic model + JSON schema round-trip both;
* ``mark_greenlit`` stamps + persists the marker (ISO-8601 UTC);
* ``mark_greenlit`` on an absent manifest fails loudly;
* default-off byte-identity — a non-greenlit, no-policy manifest carries none
  of the new keys, so existing manifests are unchanged;
* ``extra='forbid'`` still rejects a typo'd anomaly-policy field.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from hpc_agent._wire.fixtures.campaign_manifest import CampaignManifest
from hpc_agent.infra.time import parse_iso_utc
from hpc_agent.meta.campaign.manifest import (
    mark_greenlit,
    read_manifest,
    validate_manifest,
    write_manifest,
)

# ─── model round-trips ──────────────────────────────────────────────────────


def test_model_round_trips_new_fields() -> None:
    m = CampaignManifest.model_validate(
        {
            "manifest_schema_version": 1,
            "campaign_id": "tune_lr",
            "anomaly_policy": {
                "on_anomaly": "park",
                "resubmit_cap": 3,
                "circuit_breaker_failures": 2,
            },
            "greenlit": True,
            "greenlit_at": "2026-07-03T00:00:00+00:00",
        }
    )
    dumped = m.model_dump(mode="json")
    assert dumped["greenlit"] is True
    assert dumped["anomaly_policy"]["on_anomaly"] == "park"
    reparsed = CampaignManifest.model_validate(dumped)
    assert reparsed.greenlit_at == "2026-07-03T00:00:00+00:00"
    assert reparsed.anomaly_policy is not None
    assert reparsed.anomaly_policy.resubmit_cap == 3


def test_new_fields_default_when_absent() -> None:
    """A pre-existing manifest still validates; the model supplies defaults."""
    m = CampaignManifest(manifest_schema_version=1, campaign_id="legacy")
    assert m.greenlit is False
    assert m.greenlit_at is None
    assert m.anomaly_policy is None


def test_anomaly_policy_on_anomaly_is_constrained() -> None:
    """``on_anomaly`` is a closed enum — an off-menu value is rejected."""
    with pytest.raises(jsonschema.ValidationError):
        validate_manifest(
            {
                "manifest_schema_version": 1,
                "campaign_id": "c",
                "anomaly_policy": {"on_anomaly": "retry"},  # not surface|park
            }
        )


def test_schema_forbids_unknown_anomaly_policy_key() -> None:
    with pytest.raises(jsonschema.ValidationError):
        validate_manifest(
            {
                "manifest_schema_version": 1,
                "campaign_id": "c",
                "anomaly_policy": {"on_anomaly": "surface", "retry_cap": 3},  # typo
            }
        )


# ─── write_manifest / read_manifest round-trip ──────────────────────────────


def test_write_manifest_round_trips_anomaly_policy(tmp_path: Path) -> None:
    write_manifest(
        tmp_path,
        campaign_id="camp",
        anomaly_policy={"on_anomaly": "park", "resubmit_cap": 3},
    )
    data = read_manifest(tmp_path, "camp")
    assert data is not None
    assert data["anomaly_policy"] == {"on_anomaly": "park", "resubmit_cap": 3}


def test_write_manifest_default_is_byte_identical(tmp_path: Path) -> None:
    """A plain campaign's manifest carries NONE of the new keys — the default
    path's on-disk bytes are unchanged by this feature."""
    path = write_manifest(tmp_path, campaign_id="plain", goal="tune")
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "greenlit" not in raw
    assert "greenlit_at" not in raw
    assert "anomaly_policy" not in raw


# ─── mark_greenlit ──────────────────────────────────────────────────────────


def test_mark_greenlit_stamps_and_persists(tmp_path: Path) -> None:
    write_manifest(tmp_path, campaign_id="camp", goal="tune")
    updated = mark_greenlit(tmp_path, campaign_id="camp")

    assert updated["greenlit"] is True
    # A parseable ISO-8601 UTC timestamp was recorded.
    assert parse_iso_utc(updated["greenlit_at"]).tzinfo is not None

    # Durable: a fresh read sees the marker.
    data = read_manifest(tmp_path, "camp")
    assert data is not None
    assert data["greenlit"] is True
    assert data["greenlit_at"] == updated["greenlit_at"]


def test_mark_greenlit_records_supplied_timestamp(tmp_path: Path) -> None:
    write_manifest(tmp_path, campaign_id="camp")
    mark_greenlit(tmp_path, campaign_id="camp", at="2026-07-03T12:00:00+00:00")
    data = read_manifest(tmp_path, "camp")
    assert data is not None
    assert data["greenlit_at"] == "2026-07-03T12:00:00+00:00"


def test_mark_greenlit_missing_manifest_raises(tmp_path: Path) -> None:
    """The marker rides the spec — greenlighting a campaign with no manifest is
    a loud failure, not a silent no-op."""
    with pytest.raises(FileNotFoundError):
        mark_greenlit(tmp_path, campaign_id="never_created")


def test_mark_greenlit_preserves_existing_spec(tmp_path: Path) -> None:
    """Stamping the marker leaves the rest of the spec (goal, budget, policy)
    untouched — it only adds the two provenance keys."""
    write_manifest(
        tmp_path,
        campaign_id="camp",
        goal="tune lr",
        budget={"max_jobs": 10},
        anomaly_policy={"on_anomaly": "park"},
    )
    mark_greenlit(tmp_path, campaign_id="camp")
    data = read_manifest(tmp_path, "camp")
    assert data is not None
    assert data["goal"] == "tune lr"
    assert data["budget"]["max_jobs"] == 10
    assert data["anomaly_policy"]["on_anomaly"] == "park"
    assert data["greenlit"] is True
