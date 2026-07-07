"""``attention-queue`` — the fleet-wide digest ordered by needs-your-verdict-first.

A read-only ``query`` primitive (``docs/design/attention-queue.md`` Wave C / T4,
the ``doctor`` posture: no SSH, no side effects, ``idempotent=True``). It collects
every place a human action is the blocking edge — pending greenlights,
committed-but-unadvanced decisions, anomaly briefs, campaign completion briefs,
unsigned/stale notebook-audit sections, dead detached workers, alerts, open ssh
circuits — across one experiment (default) or the whole fleet (``fleet=True``, D3
glob discovery), orders them by the D2-REVISED rule (fan-out leverage → class →
oldest-since → tiebreak), and renders the deterministic markdown digest (D6).

Pure ordering/identity projection: **code computes the queue; no LLM
prioritization prose anywhere in the path** (D1). The queue moves NO state — it is
watermark-neutral by construction (D6): the ``mark_seen`` / acknowledgment
watermarks stay ``status-snapshot``'s job. Recomputed on every read; there is no
digest file, no cache, no served page — a persisted digest would be a second
source of truth that drifts from the journal (D6).

This file lives at the ``ops/`` *role root* (sibling to ``notebook_status.py`` /
``export_dossier.py``, NOT inside an ``ops/attention/`` package — the Wave A/B
module-path deviation, recorded in the design's drift log) because it composes
the cross-subject ``ops/attention_queue`` collectors and the ``ops/attention_render``
digest. The queue AGGREGATES existing predicates (D5); the one ordering definition
(``collect_items`` / ``collect_fleet`` + ``order_items``) is shared verbatim with
``status-snapshot``'s embedded ``attention`` field, so the two surfaces cannot
disagree.
"""

from __future__ import annotations

from pathlib import Path

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.queries.attention_queue import (
    AttentionItemModel,
    AttentionQueueResult,
    AttentionQueueSpec,
    SkippedNamespace,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.infra.time import parse_iso_utc_or_none, utcnow_iso
from hpc_agent.ops.attention_queue import (
    collect_fleet,
    collect_items,
    count_by_class,
    order_items,
)
from hpc_agent.ops.attention_render import render_queue

__all__ = ["attention_queue"]


@primitive(
    name="attention-queue",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key=None,
    cli=CliShape(
        help=(
            "The attention queue: the fleet-wide digest ordered by "
            "needs-your-verdict-first. Collects every place a human action is the "
            "blocking edge (pending greenlights, committed-but-unadvanced "
            "decisions, anomaly briefs, campaign completion briefs, unsigned/stale "
            "audit sections, dead workers, alerts, open ssh circuits) across one "
            "experiment or the whole fleet (fleet=True), orders by leverage "
            "(unblock fan-out) then class then oldest, and renders a deterministic "
            "markdown digest relayed verbatim. Read-only, no SSH, watermark-neutral "
            "(moves no state); recomputed on every read (no cache)."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=AttentionQueueSpec,
        schema_ref=SchemaRef(input="attention_queue"),
    ),
    agent_facing=True,
)
def attention_queue(*, experiment_dir: Path, spec: AttentionQueueSpec) -> AttentionQueueResult:
    """Collect, order (D2-revised), and render the attention queue.

    Single-experiment scope by default; ``spec.fleet`` widens to every experiment
    this machine has journaled (D3 glob discovery — non-creating: a wiped /
    unreadable / torn namespace is skipped and counted in ``skipped``). ``spec.now``
    optionally overrides the evaluation instant (the ``doctor`` precedent) — it sets
    the single ``computed_at`` stamp and the instant ages render against; it is
    NEVER an agent-facing knob for reshaping ages. Every collector routes through
    its one source predicate (D5); this verb adds selection + ordering + render
    ONLY, and moves no watermark (D6).

    Raises :class:`errors.SpecInvalid` if ``spec.now`` is a non-ISO-8601 string.
    """
    now = (spec.now or "").strip() or utcnow_iso()
    if parse_iso_utc_or_none(now) is None:
        raise errors.SpecInvalid(f"attention-queue: now override {spec.now!r} is not ISO-8601 UTC")

    if spec.fleet:
        collection = collect_fleet(now=now)
    else:
        collection = collect_items(Path(experiment_dir), now=now)
    ordered = order_items(collection.items, class_order=spec.class_order)

    return AttentionQueueResult(
        computed_at=now,
        items=[AttentionItemModel.model_validate(item.as_dict()) for item in ordered],
        counts=count_by_class(ordered),
        skipped=[SkippedNamespace(**entry) for entry in collection.skipped],
        render=render_queue(ordered, computed_at=now),
    )
