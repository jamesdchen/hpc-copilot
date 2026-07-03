"""Pydantic models for the ``read-decisions`` query primitive.

Reads back a run's or campaign's decision journal — the append-ordered
``y``/nudge audit trail (design §2). The record shape is the shared
:class:`DecisionRecord` authored alongside the ``append-decision`` action.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import RunIdStrict
from hpc_agent._wire.actions.decision_journal import DecisionRecord, ScopeKind


class ReadDecisionsInput(BaseModel):
    """Which scope's decision journal to read."""

    model_config = ConfigDict(extra="forbid", title="read-decisions input spec")

    scope_kind: ScopeKind
    scope_id: RunIdStrict


class ReadDecisionsResult(BaseModel):
    """A scope's decision journal, oldest first."""

    model_config = ConfigDict(extra="forbid", title="read-decisions output data")

    path: str
    records: list[DecisionRecord]
    count: int = Field(ge=0)
