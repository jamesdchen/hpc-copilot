"""Pydantic models for the ``scope-lock`` / ``scope-status`` primitives.

Wire surface over :mod:`hpc_agent.state.scopes` — the caller-tagged scope
lock state + look ledger. A *scope* is a filesystem-safe slug the framework
attaches NO vocabulary to (it is not "holdout" / "test" / "embargo" — those
are caller-owned semantics); these models carry only the tag and the lock
reason, never a role name and never a metric.

``scope-lock`` (mutate) records a lock — the SAFE direction, no authorship
bar. ``scope-status`` (query) is a pure read of the lock + look state for one
tag, or every tag found under ``.hpc/scopes/`` when the tag is omitted. The
UNLOCK direction is a HUMAN act journaled through ``append-decision``
(``block="scope-unlock"``, ``resolved.scope_action="unlock"``) so it faces the
authorship gate — it has no verb of its own here.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import RunIdStrict


class ScopeLockInput(BaseModel):
    """Lock a caller-tagged scope.

    ``scope`` reuses the ``RunIdStrict`` character class — the same
    filesystem-safe slug the state layer's ``validate_tag`` enforces, so a tag
    is a safe path segment and never escapes the ``.hpc/scopes/`` tree.
    """

    model_config = ConfigDict(extra="forbid", title="scope-lock input spec")

    scope: RunIdStrict
    # Free-text WHY the scope is being locked — stored as the decision's
    # ``response``. Non-empty: a lock without a reason is not auditable.
    reason: str = Field(min_length=1)


class ScopeLockResult(BaseModel):
    """Confirmation of a scope lock."""

    model_config = ConfigDict(extra="forbid", title="scope-lock output data")

    scope: str
    # Always True after a lock append (the state answer post-call).
    locked: bool
    # True when the scope was ALREADY locked before this append — locking is
    # idempotent-in-effect: the record is appended (audit trail) but the lock
    # STATE is unchanged.
    already_locked: bool
    # The scope decision-journal path the lock landed in.
    path: str


class ScopeStatusInput(BaseModel):
    """Which scope(s) to report — one tag, or every tag when omitted."""

    model_config = ConfigDict(extra="forbid", title="scope-status input spec")

    # Omit to report every scope found under ``.hpc/scopes/``.
    scope: RunIdStrict | None = None


class ScopeLooks(BaseModel):
    """Look counts for a scope — IDENTITY counts only, never a metric value."""

    model_config = ConfigDict(extra="forbid", title="scope look counts")

    prior_looks: int = Field(ge=0)
    distinct_lineages: int = Field(ge=0)


class ScopeStatusEntry(BaseModel):
    """The lock + look state of one scope."""

    model_config = ConfigDict(extra="forbid", title="scope status entry")

    locked: bool
    looks: ScopeLooks
    # Number of lock/unlock decision records in the scope's journal (the
    # append-only lock history length; an unlock never erases a prior lock).
    lock_history_len: int = Field(ge=0)


class ScopeStatusResult(BaseModel):
    """Lock + look state, keyed by scope tag."""

    model_config = ConfigDict(extra="forbid", title="scope-status output data")

    scopes: dict[str, ScopeStatusEntry] = Field(default_factory=dict)
