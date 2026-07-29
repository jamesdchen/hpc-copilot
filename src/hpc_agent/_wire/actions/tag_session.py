"""Pydantic models for the ``tag-session`` mutate verb (the devx seam).

Wire surface over :mod:`hpc_agent.ops.devx_tag`. The 2026-07-28
dev-loop/product separation left the wheel exactly one devx affordance: a
session marks itself as data for the dev loop to ingest — "friction here",
"good specimen", "tool gap" — as an opaque record in the per-experiment
``.hpc/devx/session_tags.jsonl`` ledger. Core writes and echoes the record;
it never reads tags back to change behavior (no gate, no journal, no
decision path touches them). The consumer is repo-side maintainer tooling.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TagSessionSpec(BaseModel):
    """Inputs to ``tag-session`` — one opaque devx tag record."""

    model_config = ConfigDict(extra="forbid", title="tag-session input spec")

    tags: list[str] = Field(
        min_length=1,
        description=(
            "One or more OPAQUE tag slugs (e.g. ['friction', 'aggregate-ux']). "
            "Core attaches no meaning — the vocabulary belongs to the dev "
            "loop's ingestion tooling, not the product. At least one required: "
            "an untagged record is unqueryable data."
        ),
    )
    note: str | None = Field(
        default=None,
        description=(
            "Optional free-text context, recorded VERBATIM (what happened, "
            "what was awkward, what to look at). Never interpreted by core."
        ),
    )
    session_id: str | None = Field(
        default=None,
        description=(
            "Optional caller-supplied session identifier so ingestion can "
            "group records from one sitting. Core mints nothing here — the "
            "product has no session concept, and inventing one for a devx "
            "ledger would be scope the separation just removed."
        ),
    )
    run_id: str | None = Field(
        default=None,
        description=(
            "Optional run id the tag is about, OPAQUE here (not validated "
            "against the runs store — a tag may name a run that failed to "
            "exist; that fact is itself data)."
        ),
    )

    @field_validator("tags")
    @classmethod
    def _tags_nonempty(cls, value: list[str]) -> list[str]:
        if any(not tag.strip() for tag in value):
            raise ValueError("tags entries must be non-empty strings")
        return value


class TagSessionResult(BaseModel):
    """Echo of the appended record plus the ledger location."""

    model_config = ConfigDict(extra="forbid", title="tag-session output data")

    path: str = Field(
        description="Absolute path of the session-tag ledger the record was appended to.",
    )
    record: dict[str, Any] = Field(
        description=(
            "The record as written (ts stamped at append time, UTC ISO-8601) "
            "— echoed so the session can relay exactly what the ledger now "
            "holds."
        ),
    )
    count: int = Field(
        description="Total parseable records in the ledger after the append.",
    )
