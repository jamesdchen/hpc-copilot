"""The deterministic markdown digest for the attention queue (D6).

Pure string work — no I/O, no journal reads, no ``_wire`` import (the
``ops/relay_render.py`` posture, and a natural sibling of the notebook-audit and
run-story renderers). The caller hands in the ALREADY-ORDERED items (D2) plus the
single ``computed_at`` stamp; this composes the digest. Same inputs → byte-
identical markdown.

Layout (D6): a header line carrying the one ``computed_at`` stamp and the re-run
remedy, a counts line, then one section per class in the order the classes appear
in the ordered items (so a ``class_order`` override is reflected), one line per
item::

    <age> · <kind> · <scope_id>[ on <cluster>] — <action or evidence one-liner>

Ages are rendered as durations RELATIVE to ``computed_at`` — so an overnight
digest read at noon is visibly a 6am projection. No cross-source age is ever
INTERPRETED as a judgment (no "URGENT"); age is a duration only. The wording is
composed from each item's OWN fields, never free prose (D1's no-LLM-prose rule).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent.infra.time import parse_iso_utc_or_none
from hpc_agent.ops.attention_queue import (
    AUDIT_SECTION_STALE,
    AUDIT_SECTION_UNSIGNED,
    CAMPAIGN_PENDING,
    GREENLIGHT_UNADVANCED,
    RUN_PARKED,
    RUN_STALLED,
    count_by_class,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from hpc_agent.ops.attention_queue import AttentionItem

__all__ = ["render_queue"]


def _format_age(seconds: float) -> str:
    """Coarse, deterministic duration: ``s`` / ``m`` / ``h`` / ``d``, floored."""
    total = int(seconds)
    if total < 0:
        total = 0
    if total < 60:
        return f"{total}s"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    return f"{hours // 24}d"


def _age(since: str | None, computed_at: str) -> str:
    """The age of *since* relative to *computed_at*, or ``"age?"`` when undated."""
    start = parse_iso_utc_or_none(since)
    now = parse_iso_utc_or_none(computed_at)
    if start is None or now is None:
        return "age?"
    return _format_age((now - start).total_seconds())


def _oneline(text: str) -> str:
    """Collapse a possibly multi-line source string to one whitespace-normal line."""
    return " ".join(text.split())


def _detail(item: AttentionItem) -> str:
    """The item's one-liner: the source's own ``action`` string, else a compact
    phrase composed from the item's OWN fields (never authored prose).
    """
    if item.action:
        return _oneline(item.action)
    if item.kind == RUN_STALLED:
        due = item.evidence.get("next_tick_due")
        return f"driver stalled; next tick was due {due}" if due else "driver stalled"
    if item.kind == RUN_PARKED:
        return (
            f"parked awaiting your decision at {item.block}"
            if item.block
            else ("parked awaiting your decision")
        )
    if item.kind == GREENLIGHT_UNADVANCED:
        return (
            f"approved but the driver has not advanced at {item.block}"
            if item.block
            else ("approved but the driver has not advanced")
        )
    if item.kind == CAMPAIGN_PENDING:
        return (
            f"campaign awaiting your response at {item.block}"
            if item.block
            else ("campaign awaiting your response")
        )
    if item.kind == AUDIT_SECTION_UNSIGNED:
        return f"section {item.block} unsigned"
    if item.kind == AUDIT_SECTION_STALE:
        return f"section {item.block} sign-off stale"
    return str(item.kind)


def _item_line(item: AttentionItem, computed_at: str) -> str:
    """One rendered item line (D6).

    The D2-revision fan-out rides the line as an HONEST count — ``unblocks N`` —
    only when the item has an encoded downstream edge (``unblocks > 0``). It is a
    counted leverage fact, never urgency prose (no "URGENT" / "critical"): the
    reader sees WHY it sorted high without the code asserting a judgment.
    """
    where = f" on {item.cluster}" if item.cluster else ""
    scope = item.scope_id or "?"
    leverage = f" · unblocks {item.unblocks}" if item.unblocks else ""
    return (
        f"- {_age(item.since, computed_at)} · {item.kind} · "
        f"{scope}{where}{leverage} — {_detail(item)}"
    )


def _counts_line(items: Sequence[AttentionItem]) -> str:
    """The header counts line: ``"N item(s): a blocked, b verdict, c informational"``."""
    counts = count_by_class(items)
    total = len(items)
    if not counts:
        return f"{total} item(s)"
    parts = ", ".join(f"{n} {cls}" for cls, n in counts.items())
    return f"{total} item(s): {parts}"


def render_queue(items: Sequence[AttentionItem], *, computed_at: str) -> str:
    """Render the ordered *items* as the deterministic markdown digest (D6).

    *items* MUST already be in the D2 total order (the caller ordered them so ONE
    ordering definition serves render + verb + snapshot). *computed_at* is the
    single stamp the whole projection is dated by and every age is measured
    against. Sections are emitted in the order classes first appear in *items*, so
    a ``class_order`` override flows through to the render. Pure — same inputs yield
    byte-identical output.
    """
    lines: list[str] = [
        f"attention queue · computed {computed_at} · re-run for current state",
        "",
        _counts_line(items),
        "",
    ]

    if not items:
        lines.append("(nothing needs your attention)")
        return "\n".join(lines).rstrip() + "\n"

    current_class: str | None = None
    for item in items:
        if item.item_class != current_class:
            if current_class is not None:
                lines.append("")  # blank line between sections
            current_class = item.item_class
            lines.append(f"## {current_class}")
            lines.append("")
        lines.append(_item_line(item, computed_at))
    # A trailing blank between the last item and EOF is normalized away so the
    # digest is byte-stable regardless of the final class.
    return "\n".join(lines).rstrip() + "\n"
