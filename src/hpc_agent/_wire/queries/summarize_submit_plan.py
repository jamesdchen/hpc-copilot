"""Pydantic model for the ``summarize-submit-plan`` query atom's output."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SummarizeSubmitPlanResult(BaseModel):
    """Canonical pre-submit confirmation summary.

    Slash-command Step 5 prints headline + body verbatim and asks
    confirm_prompt. Byte-stable for the same input.
    """

    model_config = ConfigDict(extra="forbid", title="summarize-submit-plan output")

    headline: str
    body: str
    confirm_prompt: str = Field(
        description="Literal 'Confirm? [y/N]' or magnitude-warning variant when total_tasks > 1000.",
    )
