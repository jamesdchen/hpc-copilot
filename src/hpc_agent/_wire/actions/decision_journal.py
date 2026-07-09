"""Pydantic models for the ``append-decision`` primitive (decision journal).

Wire surface for the human-amplification decision journal
(``docs/design/human-amplification-blocks.md`` §2, resolving open TODO
§8#4 — "what a recorded ``y``/nudge exchange persists"). Mirrors the
plain-dict record that :func:`hpc_agent.state.decision_journal.append_decision`
writes; the ``ops`` primitive validates the caller's payload against
:class:`AppendDecisionInput` and re-surfaces the persisted line as a
:class:`DecisionRecord`.

``DecisionRecord`` is the persisted shape — it is also reused by the
``read-decisions`` query result (``_wire/queries/decision_journal.py``).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import RunIdStrict

# A decision belongs to a run, a campaign, a named scope, or a notebook
# audit. ``run`` journals the submit S1–S4 / anomaly / harvest touchpoints
# of a single run; ``campaign`` journals the once-at-start spec greenlight
# plus anomaly / completion briefs of an asynchronous campaign (design §4);
# ``scope`` journals the lock/unlock touchpoints of a caller-tagged
# experiment scope (``hpc_agent.state.scopes``); ``notebook`` journals the
# sign-off touchpoints of an audited source module under a caller-authored
# ``audit_id`` (``docs/design/notebook-audit.md`` D3); ``registration`` journals
# the deployment-boundary attestation touchpoints of a caller-authored
# ``registration_id`` (``docs/design/registration-kernel.md`` R9 — the
# ``registration`` / ``registration-revoke`` records gated by R6); ``conclusion``
# journals the human-authored finding touchpoints of evidence memory under a
# caller-authored ``conclusion_id`` (``docs/design/evidence-memory.md`` E-shape —
# the ``conclusion`` / ``conclusion-revoke`` records gated by the E-shape locks);
# ``challenge`` journals the human-authored structured-dissent touchpoints under a
# caller-authored ``challenge_id`` (``docs/design/challenge-attestation.md``
# C-shape — the ``challenge`` / ``challenge-verdict`` / ``challenge-withdraw``
# records gated by the C-gate locks).
# Kept in lockstep with ``state.decision_journal.SCOPE_KINDS`` (schema regen is the
# integrator's job; the ScopeKind literal change regenerates schemas).
ScopeKind = Literal[
    "run", "campaign", "scope", "notebook", "registration", "pack", "conclusion", "challenge"
]

# The evidence the proposal was drafted over — an opaque free-text digest
# OR a structured dict. The journal never interprets it; it round-trips it
# so the audit answers "what did the human see when they decided?".
EvidenceDigest = str | dict[str, Any]

# The LLM-drafted proposal: free text, a structured object, OR a list of
# options (the "set of interpretation options" case, design §2).
Proposal = str | list[Any] | dict[str, Any]


class AppendDecisionInput(BaseModel):
    """One ``y``/nudge exchange to append to a scope's decision journal.

    Everything the design §2 schema persists, minus the two fields the
    primitive/state layer owns: ``ts`` (auto-stamped UTC ISO-8601) and
    ``schema_version`` (a constant). The agent supplies only what it
    resolved at the touchpoint.
    """

    model_config = ConfigDict(extra="forbid", title="append-decision input spec")

    # Which store, and the run_id / campaign_id it belongs to. ``scope_id``
    # reuses the ``RunIdStrict`` character class — CampaignId shares the same
    # filesystem-safe pattern, and the id becomes a path segment.
    scope_kind: ScopeKind
    scope_id: RunIdStrict

    # Free-text block-terminator id that raised this decision point — e.g.
    # "submit.S1", "submit.S2", "campaign.spec", "anomaly", "harvest".
    block: str = Field(min_length=1)

    # The code-digested evidence the proposal was drafted over (opaque).
    evidence_digest: EvidenceDigest = ""

    # The LLM-drafted proposal — text and/or a list of options.
    proposal: Proposal = ""

    # The human's answer: the literal "y" for greenlight, OR the
    # natural-language nudge text. ``"y"`` is the greenlight sentinel by
    # protocol; anything else is a nudge.
    response: str = Field(min_length=1)

    # The resulting decision as structured data. On a greenlight this
    # carries the settled decision; on a nudge (the loop continues) it is
    # typically empty — the exchange re-drafts rather than resolving.
    resolved: dict[str, Any] = Field(default_factory=dict)

    # Optional provenance: who/how (e.g. {"decided_by": "human",
    # "surface": "slash", "session": "..."}).
    provenance: dict[str, Any] = Field(default_factory=dict)


class DecisionRecord(BaseModel):
    """One persisted decision-journal line, as read back from disk.

    ``extra="allow"`` so an additive schema bump (a new field written by a
    newer writer) does not break an older reader — the same forward-compat
    posture ``load-context``'s nested rows take against evolving sidecar
    schemas.
    """

    model_config = ConfigDict(extra="allow", title="decision journal record")

    schema_version: int
    ts: str
    scope_kind: ScopeKind
    scope_id: RunIdStrict
    block: str
    response: str
    evidence_digest: EvidenceDigest = ""
    proposal: Proposal = ""
    resolved: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)


class AppendDecisionResult(BaseModel):
    """Confirmation of one appended decision record."""

    model_config = ConfigDict(extra="forbid", title="append-decision output data")

    path: str
    record: DecisionRecord
    # Total records in the journal after this append (1 for the first).
    count: int = Field(ge=1)
