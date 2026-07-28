"""Pydantic models for the ``read-decisions`` query primitive.

Reads back a run's or campaign's decision journal — the append-ordered
``y``/nudge audit trail (design §2). The record shape is the shared
:class:`DecisionRecord` authored alongside the ``append-decision`` action.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_serializer

from hpc_agent._wire._shared import RunIdStrict
from hpc_agent._wire.actions.decision_journal import DecisionRecord, ScopeKind


class ReadDecisionsInput(BaseModel):
    """Which scope's decision journal to read."""

    model_config = ConfigDict(extra="forbid", title="read-decisions input spec")

    scope_kind: ScopeKind
    scope_id: RunIdStrict
    digest: bool = Field(
        default=False,
        description=(
            "F5 (context-footprint plan): true serves records_digest — per-record "
            "metadata (ts, block, attestor, response sha12 + length, resolved key "
            "names) with the record BODIES omitted (records comes back empty). The "
            "chain-coherence preflight scan needs ordering + identity + counts, "
            "never the prose; pass false (the default) only when a record's full "
            "text is actually needed — the branchpoint rule."
        ),
    )


class DecisionRecordDigest(BaseModel):
    """One record's identity/ordering metadata — everything the preflight scan
    reads, none of the prose. ``response_sha12`` fingerprints the response text
    (verifiable against the full record); ``resolved_keys`` names the resolved
    mapping's keys without its values."""

    model_config = ConfigDict(extra="forbid", title="read-decisions record digest")

    ts: str
    block: str
    response_sha12: str
    response_chars: int = Field(ge=0)
    resolved_keys: list[str]
    attestor_id: str | None = None


class ReadDecisionsResult(BaseModel):
    """A scope's decision journal, oldest first.

    Digest mode (spec ``digest: true``) fills ``records_digest`` and leaves
    ``records`` EMPTY; the default fills ``records`` and OMITS
    ``records_digest`` entirely (the serializer drops the ``None`` — the
    default response stays byte-identical to the pre-digest wire shape).
    ``count`` is the journal's record count in both modes.
    """

    model_config = ConfigDict(extra="forbid", title="read-decisions output data")

    path: str
    records: list[DecisionRecord]
    count: int = Field(ge=0)
    records_digest: list[DecisionRecordDigest] | None = None

    @model_serializer(mode="wrap")
    def _omit_absent_digest(self, handler: Any) -> Any:
        out = handler(self)
        if isinstance(out, dict) and out.get("records_digest") is None:
            out.pop("records_digest", None)
        return out
