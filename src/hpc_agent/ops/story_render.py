"""Render the run story — canonical JSON + ``story_sha`` + code-rendered markdown.

Design: ``docs/design/run-story.md`` (Wave A / T2, decision D4). Two artifacts
from one projection, mirroring the audit view
(:func:`~hpc_agent.ops.notebook.audit_view._canonical_json` / its ``view_sha``):

1. **canonical JSON** — the header + the ordered (already windowed) event list +
   the ``total_events`` / ``omitted_count`` honesty counts, serialized with
   sorted keys; ``story_sha = sha256(canonical_json)``. The counts ride the
   pre-image, so a windowed sha can never be passed off as covering events it
   does not contain (D6).
2. **markdown** — a deterministic pure-string rendering OF that JSON, one line
   per event, grouped under order-preserving ``kind`` (block-phase) headings,
   with an explicit "showing N of M events (K older events omitted)" header
   whenever a window applied. NO reordering (that would fork the timeline — the
   boundary flag), NO LLM prose.

``story_sha`` is a FINGERPRINT, not an attestation — deliberately NOT routed
through :mod:`hpc_agent.state.attestation`; nothing attests a story.

Pure string work — no journal reads, no I/O, no ``_wire`` import (the
:mod:`hpc_agent.ops.relay_render` posture; deliberately NOT folded into
``relay_render.py``, whose contract is the one-liner relay). The evidence a line
renders is filtered to sha POINTERS + COUNTS only: a metric VALUE that somehow
reached an event never reaches the human-facing line (D3, the counts-only rule).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hpc_agent.state.run_story import StoryEvent

__all__ = [
    "EVENT_KEYS",
    "StoryRender",
    "event_payload",
    "story_payload",
    "story_sha",
    "render_markdown",
    "render_story",
]

#: The exact D3 event key set — every event dict construction carries these and
#: nothing else (pinned by the T5 boundary test).
EVENT_KEYS: tuple[str, ...] = ("ts", "stream", "actor", "kind", "subject_id", "evidence", "text")

# The evidence keys a rendered line may show: sha POINTERS + COUNTS + a small
# set of identity/state literals — never a caller metric. A key outside this
# whitelist (a crafted ``accuracy`` / ``loss`` value) is DROPPED from the line,
# so a metric value can never render (the counts-only rule, D3). The full
# evidence still rides the canonical JSON / ``story_sha`` — the whitelist is a
# render-surface guard, and the producers (state/run_story.py) already emit only
# safe keys.
_EVIDENCE_KEEP_EXACT: frozenset[str] = frozenset(
    {
        "scope",
        "scope_action",
        "reducer_block",
        "stage_reached",
        "decided_by",
        "superseded_by",
        "ts_missing",
        "section",
        "error",
        # Class-C2 overnight-finding identity/classification literals (never a
        # metric value): the cause slug, the heal class, and the report-only
        # disposition (state/run_story.py::project_c2_findings).
        "cause",
        "heal_class",
        "disposition",
    }
)
_EVIDENCE_KEEP_SUFFIXES: tuple[str, ...] = ("_sha", "_digest", "_count", "_root")


def _keep_evidence_key(key: str) -> bool:
    return key in _EVIDENCE_KEEP_EXACT or key.endswith(_EVIDENCE_KEEP_SUFFIXES)


def _plainify(obj: Any) -> Any:
    """Coerce to JSON-native structures (mappings→sorted dict, sequences→list).

    Keeps str/bytes as leaves (never treated as sequences), so a header or
    evidence value embeds in the canonical payload without interpretation — the
    :func:`~hpc_agent.ops.notebook.audit_view._plainify` precedent.
    """
    if isinstance(obj, Mapping):
        return {str(k): _plainify(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plainify(v) for v in obj]
    return obj


def _canonical_json(obj: Any) -> str:
    """Canonical JSON: sorted keys, compact separators, unicode kept as-is.

    The one serialization ``story_sha`` is taken over — deterministic and
    platform-stable regardless of dict insertion order.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def event_payload(event: StoryEvent) -> dict[str, Any]:
    """Project one :class:`~hpc_agent.state.run_story.StoryEvent` to its JSON dict.

    The single event-dict construction site — exactly the :data:`EVENT_KEYS` set
    (D3), evidence plainified for canonical serialization. No key is added or
    dropped here; the render-surface whitelist applies only to the MARKDOWN.
    """
    return {
        "ts": event.ts,
        "stream": event.stream,
        "actor": event.actor,
        "kind": event.kind,
        "subject_id": event.subject_id,
        "evidence": _plainify(event.evidence),
        "text": event.text,
    }


def story_payload(
    header: Mapping[str, Any],
    events: Sequence[StoryEvent],
    *,
    total_events: int,
    omitted_count: int,
) -> dict[str, Any]:
    """Assemble the canonical story payload (the pre-image ``story_sha`` covers).

    *events* is the already-windowed ordered list; *total_events* is the FULL
    count before any window, *omitted_count* the number a window dropped. Both
    counts ride the payload so the sha is honest about coverage (D6).
    """
    return {
        "header": _plainify(header),
        "events": [event_payload(e) for e in events],
        "total_events": int(total_events),
        "omitted_count": int(omitted_count),
    }


def story_sha(payload: Mapping[str, Any]) -> str:
    """sha256 hexdigest of the canonical JSON of *payload* (the story fingerprint)."""
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


# ── the code-rendered markdown projection ─────────────────────────────────────


def _scalar(value: Any) -> str:
    """Render an evidence scalar for the line (dict/list fall back to canonical JSON)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (Mapping, list, tuple)):
        return _canonical_json(_plainify(value))
    return str(value)


def _evidence_phrase(evidence: Mapping[str, Any]) -> str:
    """Render the whitelisted evidence pointers as ``k=v, k=v`` (or ``""``).

    Only sha pointers + counts + identity literals survive :func:`_keep_evidence_key`;
    a crafted metric value is dropped, so it can never reach the human-facing
    line (the counts-only rule, D3).
    """
    parts = [f"{k}={_scalar(evidence[k])}" for k in sorted(evidence) if _keep_evidence_key(k)]
    return ", ".join(parts)


def _event_line(payload: Mapping[str, Any]) -> str:
    """One timeline line: ``ts · actor · kind · subject [· evidence] [· "text"]``."""
    ts = str(payload.get("ts") or "?")
    segs: list[str] = [
        ts,
        str(payload.get("actor") or ""),
        str(payload.get("kind") or ""),
        str(payload.get("subject_id") or ""),
    ]
    evidence = payload.get("evidence")
    phrase = _evidence_phrase(evidence) if isinstance(evidence, Mapping) else ""
    if phrase:
        segs.append(phrase)
    text = payload.get("text")
    if isinstance(text, str) and text:
        segs.append(f'"{text}"')
    return "- " + " · ".join(segs)


def render_markdown(payload: Mapping[str, Any]) -> str:
    """Render *payload* as deterministic, code-authored markdown (D4 / D6 posture).

    Pure formatting of the payload's own fields — the header, the window-honesty
    header when a window applied, and one line per event grouped under
    order-preserving ``kind`` (block-phase) headings. Same payload →
    byte-identical markdown. NO reordering, NO LLM prose.
    """
    header = payload.get("header")
    header = header if isinstance(header, Mapping) else {}
    events = payload.get("events")
    events = events if isinstance(events, list) else []
    total = int(payload.get("total_events") or 0)
    omitted = int(payload.get("omitted_count") or 0)

    lines: list[str] = ["# Run story", ""]
    if omitted > 0:
        shown = total - omitted
        lines.append(f"- showing {shown} of {total} events ({omitted} older events omitted)")
    else:
        lines.append(f"- {total} event(s)")
    for key in sorted(header):
        value = header[key]
        if isinstance(value, (list, tuple)):
            rendered = ", ".join(str(v) for v in value)
        else:
            rendered = str(value)
        lines.append(f"- {key}: {rendered}")
    lines.append("")

    if not events:
        lines.append("(no events)")
        return "\n".join(lines).rstrip() + "\n"

    current_kind: str | None = None
    for ev in events:
        ev = ev if isinstance(ev, Mapping) else {}
        kind = str(ev.get("kind") or "")
        if kind != current_kind:
            current_kind = kind
            if lines and lines[-1] != "":
                lines.append("")
            lines.append(f"## {kind or '(unknown)'}")
            lines.append("")
        lines.append(_event_line(ev))
    return "\n".join(lines).rstrip() + "\n"


@dataclass(frozen=True)
class StoryRender:
    """The two rendered artifacts + the fingerprint over the canonical JSON.

    * ``payload`` — the JSON-native story dict ``story_sha`` is taken over.
    * ``story_sha`` — ``sha256`` of the canonical JSON (a fingerprint, not an
      attestation).
    * ``markdown`` — the code-rendered timeline (``""`` when the caller opts
      out).
    """

    payload: Mapping[str, Any]
    story_sha: str
    markdown: str


def render_story(
    header: Mapping[str, Any],
    events: Sequence[StoryEvent],
    *,
    total_events: int,
    omitted_count: int,
    markdown: bool = True,
) -> StoryRender:
    """Build the payload, fingerprint it, and (optionally) render the markdown.

    The one-call entry point the ``ops`` layer (T4) uses: it hands in the header
    it assembled and the already-windowed events + honesty counts, and receives
    the canonical payload, ``story_sha``, and markdown. Pure — same inputs yield
    the same artifacts on every platform.
    """
    payload = story_payload(header, events, total_events=total_events, omitted_count=omitted_count)
    return StoryRender(
        payload=payload,
        story_sha=story_sha(payload),
        markdown=render_markdown(payload) if markdown else "",
    )
