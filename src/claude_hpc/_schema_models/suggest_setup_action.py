"""Pydantic model for the ``suggest-setup-action`` query atom's output."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class SuggestSetupActionResult(BaseModel):
    """Recommended /submit-hpc Setup branch.

    Agent branches on ``action``; ``candidates`` carries the
    relevant runs/sidecars to surface to the user. The
    ``recommended_run_id`` field is named with a 'recommended_'
    prefix so the schema-defs consistency check (which forbids
    nullable run_id) doesn't trip.
    """

    model_config = ConfigDict(title="suggest-setup-action output")

    priority: Literal[0, 1, 2, 3]
    action: Literal["monitor", "reuse", "interview", "fresh"]
    recommended_run_id: str | None = Field(
        description="Recommended single best candidate (newest by submitted_at / mtime); null at priority 2/3.",
    )
    candidates: list[dict[str, Any]] = Field(
        description="Full candidate list at this priority. Shape varies: priority 0 carries journal RunRecord summaries; priority 1 carries sidecar dicts.",
    )
    reason: str
