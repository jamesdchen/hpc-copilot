"""Pydantic model for ``campaign-status`` / ``campaign-list`` envelope data.

The single ``campaign.output.json`` schema is shared between the
two CLI subcommands — ``campaign status`` returns the
``status_data`` shape, ``campaign list`` returns the ``list_data``
shape. Pydantic emits the union as a top-level ``anyOf`` (no
discriminator since the two shapes have different required keys
rather than a tag field).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class CampaignStatusData(BaseModel):
    """Returned by ``hpc-agent campaign status --campaign-id <id>``."""

    model_config = ConfigDict(extra="forbid")

    campaign_id: str = Field(min_length=1)
    iterations: int = Field(ge=0)
    in_flight: int | None = Field(default=None, ge=0)
    history: list[dict[str, Any]] = Field(
        description="Per-iteration reduced metrics dicts, oldest-first.",
    )
    run_ids: list[str] | None = Field(
        default=None,
        description="Sidecars matching this campaign, oldest-first.",
    )


class _CampaignListEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    campaign_id: str
    iterations: int = Field(ge=1)


class CampaignListData(BaseModel):
    """Returned by ``hpc-agent campaign list``."""

    model_config = ConfigDict(extra="forbid")

    campaigns: list[_CampaignListEntry]


# The wire schema is the union; the CLI loads the same file for both
# subcommands and the framework picks the correct shape based on which
# one was invoked.
CampaignAdapter: TypeAdapter[CampaignStatusData | CampaignListData] = TypeAdapter(
    CampaignStatusData | CampaignListData
)
