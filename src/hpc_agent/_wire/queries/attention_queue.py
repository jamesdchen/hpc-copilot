"""Pydantic models for the ``attention-queue`` query (D7).

The attention queue is the fleet-wide digest ordered by *needs-your-verdict-first*
(``docs/design/attention-queue.md``): every place in the system where a human
action is the blocking edge — pending greenlights, committed-but-unadvanced
decisions, anomaly briefs, campaign completion briefs, unsigned/stale
notebook-audit sections, dead detached workers, alerts, open ssh circuits —
collected across every run, campaign, and audit, ordered by a deterministic
code-computed rule (D2), rendered as a deterministic markdown digest (D6).

Pure ordering/identity projection: **code computes the queue; no LLM
prioritization prose anywhere in the path** (D1's no-urgency-score decision).
The spec carries NO ``mark_seen`` field — the queue is watermark-neutral by
design (D6): absence of the affordance, not a default. ``now`` is the
deterministic-testing override (the ``doctor`` precedent), never an agent-facing
knob for reshaping ages.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AttentionQueueSpec(BaseModel):
    """Input spec for the ``attention-queue`` verb (D7)."""

    model_config = ConfigDict(extra="forbid", title="attention-queue input spec")

    fleet: bool = Field(
        default=False,
        description=(
            "When False (default), scope is the single experiment_dir. When True, "
            "widen to every experiment this machine has ever journaled — glob the "
            "journal home for '*/repo.json', recover each experiment root, and run "
            "the identical per-experiment collection there (D3). A namespace whose "
            "experiment_dir no longer exists / is unreadable / is torn is skipped "
            "silently and counted in 'skipped'."
        ),
    )
    class_order: list[str] | None = Field(
        default=None,
        description=(
            "Optional class-sequence override (D2, the T12 attention_order "
            "precedent): listed classes ('blocked' / 'verdict' / 'informational') "
            "first in the given order, UNKNOWN names ignored (not refused), "
            "unlisted classes keep the default order after them. The default is "
            "'blocked, verdict, informational'. This overrides ONLY the class "
            "sequence — the within-class rule (oldest-since first, then "
            "(kind, scope_id)) is fixed, never overridable (re-ranking individual "
            "items would be a caller doing prioritization prose)."
        ),
    )
    now: str | None = Field(
        default=None,
        description=(
            "Optional ISO-8601 UTC 'now' override for deterministic testing (the "
            "doctor precedent). When omitted, the current time is used. It sets the "
            "queue's computed_at stamp and the instant ages are rendered against — "
            "never an agent-facing knob for reshaping ages."
        ),
    )


class AttentionSubject(BaseModel):
    """The subject a queue item points at (D1).

    ``scope_kind`` is one of the decision-journal scope kinds
    (``state/decision_journal.py::SCOPE_KINDS`` — run / campaign / scope /
    notebook) for a scoped item, or null for a fleet-level infra signal (an
    ``alert`` or an ``ssh-circuit-open``, which carry no run/campaign/scope/
    notebook subject). The queue introduces ZERO new domain vocabulary.
    """

    model_config = ConfigDict(extra="forbid", title="attention-queue subject")

    scope_kind: str | None = Field(
        default=None,
        description="run / campaign / scope / notebook, or null for a fleet-level signal.",
    )
    scope_id: str = Field(
        description="The subject id (run_id / campaign_id / audit_id / host / alert ts)."
    )
    block: str | None = Field(
        default=None,
        description="The block / section slug the item is scoped to, when the source carries one.",
    )


class AttentionItemModel(BaseModel):
    """One queue item: identity + class + evidence pointer, never a score (D1).

    Priority is expressed ONLY as ``item_class`` (the D2 class) plus position in
    the D2 total order — both recomputable from the record, neither asserted.
    There is deliberately no urgency-score field (a number the code cannot
    justify invites the LLM to re-rank by it).
    """

    model_config = ConfigDict(
        extra="forbid",
        title="attention-queue item",
        populate_by_name=True,
        # Every dump emits the wire key ``class`` (the alias), not the Python
        # field name — the CLI envelope and the fuzz harness both dump WITHOUT
        # by_alias, and the baked schema (correctly) forbids ``item_class``.
        # Linux-CI fuzz caught the divergence on 1ca77f53.
        serialize_by_alias=True,
    )

    kind: str = Field(
        description=(
            "Opaque kind string: greenlight-unadvanced / run-parked / run-stalled "
            "/ run-anomaly / dead-worker / campaign-pending / audit-section-"
            "unsigned / audit-section-stale / alert / ssh-circuit-open."
        )
    )
    item_class: str = Field(
        alias="class",
        description="blocked / verdict / informational (D2) — the ordering class.",
    )
    subject: AttentionSubject = Field(description="Who/what this item is about (D1).")
    experiment_dir: str = Field(
        description="Which experiment this item belongs to (fleet mode disambiguator)."
    )
    cluster: str | None = Field(
        default=None, description="Where, when the subject has a cluster; else null."
    )
    since: str | None = Field(
        default=None,
        description=(
            "ISO-8601 ts when this item's condition began, read from the SOURCE "
            "record (awaiting_since / last_tick_at / decision ts / alert ts); null "
            "when the source carries no timestamp. Ordering input only — never "
            "interpreted as a judgment."
        ),
    )
    action: str | None = Field(
        default=None,
        description=(
            "The source predicate's OWN drafted proposal/note string (the dead-"
            "worker re-invoke proposal, the anomaly recommendation's action DATA); "
            "null when the source drafts none. The queue NEVER authors one."
        ),
    )
    unblocks: int = Field(
        default=0,
        description=(
            "The D2-revision LEVERAGE key (user, 2026-07-08): the count of pending "
            "downstream subjects that become actionable when this one verdict "
            "clears, COUNTED over the dependency edges the journals already encode "
            "(a committed-unadvanced greenlight → its run; an unsigned/stale audit "
            "section → the module's passed gate → every run whose sidecar "
            "audited_source echo names the audit; a campaign-pending verdict → the "
            "campaign's remaining runs). Never a score — where no encoded edge "
            "exists it is 0 and the item falls through to the class order. The "
            "primary sort key (fan-out descending)."
        ),
    )
    evidence: dict[str, Any] = Field(
        default_factory=dict,
        description="The source's own structured evidence dict, passed through opaque.",
    )


class SkippedNamespace(BaseModel):
    """One namespace / audit skipped during collection, with the reason (D3).

    A wiped demo repo, an unreadable/torn ``repo.json``, or an audit with no
    resolvable ``audited_source`` opt-in must never crash the morning read — it
    is skipped silently and counted here (fail-open discipline).
    """

    model_config = ConfigDict(extra="forbid", title="attention-queue skipped entry")

    ref: str = Field(description="The repo_hash / audit_id / campaign_id that was skipped.")
    reason: str = Field(description="Why it was skipped (unreadable, no opt-in, torn, absent).")


class AttentionQueueResult(BaseModel):
    """Shape of the ``data`` field on an ``attention-queue`` envelope (D7).

    ``render`` rides the result the way ``relay`` rides ``StatusBlockResult`` —
    the agent relays it VERBATIM. The single ``computed_at`` stamp dates the whole
    projection (D6): an overnight digest read at noon is visibly a 6am projection,
    and the remedy is stated in the render header (re-run the verb).
    """

    model_config = ConfigDict(extra="forbid", title="attention-queue output data")

    computed_at: str = Field(
        description="The single instant the queue was computed against (ISO-8601 UTC)."
    )
    items: list[AttentionItemModel] = Field(
        default_factory=list,
        description=(
            "Queue items in the D2-REVISED total order: fan-out (unblocks) "
            "descending, then class order, then oldest-since, then (kind, scope_id)."
        ),
    )
    counts: dict[str, int] = Field(
        default_factory=dict,
        description="Item count per class ({blocked: n, verdict: m, informational: k}).",
    )
    skipped: list[SkippedNamespace] = Field(
        default_factory=list,
        description="Namespaces / audits skipped during collection (fail-open accounting).",
    )
    render: str = Field(
        description="The deterministic markdown digest — relayed to the human verbatim."
    )
