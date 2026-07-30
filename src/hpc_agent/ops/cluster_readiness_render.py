"""Deterministic markdown render of the standing readiness ledger.

Pure string work — no I/O, no journal reads, no ``_wire`` import (the
``ops/attention_render.py`` posture). The caller hands in already-collected
entries plus the single ``computed_at`` stamp; this composes the digest. Same
inputs → byte-identical markdown.

Two rules the render exists to enforce:

* **Every atom carries its age.** A verdict without an age is the failure mode
  the ledger was built to remove (S2 discovering at fire time that a "known
  good" route was known good yesterday). Age is rendered against ONE stamp, and
  a stale atom is marked ``STALE`` inline — never quietly dropped.
* **Absence is rendered.** An atom kind nothing has fed prints ``unknown`` with
  its reason, so no reader can mistake an unfed invariant for a green one.

The render authors no advice and no urgency words: it states verdicts, ages and
provenance, all composed from the entry's own fields.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["render_readiness"]

#: One line of prose per overall verdict, stating what the evidence supports —
#: never what to do about it. Keyed by the fixed vocabulary so an unmapped
#: verdict is structurally impossible to render.
_VERDICT_GLOSS: dict[str, str] = {
    "ready": "every recorded invariant is green and fresh",
    "stale": "no fresh failure, but the evidence does not support ready",
    "degraded": "a fresh observation says an invariant is broken",
    "unknown": "nothing has ever been observed here",
}

#: The ``unknown`` gloss when the ledger EXISTED but could not be read. The
#: default gloss would say "nothing has ever been observed here" two lines above
#: a disclosure saying the file is corrupt — two contradictory claims in one
#: entry, and the reassuring one comes first (2026-07-30 review, F7). Whatever
#: was observed is unrecoverable, which is a different fact from never looking.
_CORRUPT_GLOSS = "the ledger exists but could not be read, so nothing is known from it"

#: Why an atom is unknown — the render says which, so "unfed" and "never
#: observed for this host" are not confused with each other.
_UNKNOWN_REASON = "no observation recorded"


def _age_phrase(age_seconds: int | None) -> str:
    """``37m ago`` / ``age unknown`` — the age half of every atom line."""
    if age_seconds is None:
        return "age unknown"
    s = max(0, int(age_seconds))
    if s < 60:
        return f"{s}s ago"
    minutes = s // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours, rem = divmod(minutes, 60)
    return f"{hours}h {rem}m ago" if rem else f"{hours}h ago"


def _subject(atom: dict[str, Any]) -> str:
    """``connect/effective → login.example.edu`` — the atom's full subject.

    An atom's identity in the ledger is ``(sensor, route, target)``, and the
    render must show ALL THREE or two distinct atoms print the same line. The
    route is part of the subject, not the evidence: ``preamble`` failing on the
    effective route while passing on the direct one is a different fact from
    failing on both, and collapsing them is exactly the 2026-07-30 misread this
    substrate exists to prevent. ``target`` is part of it for the same reason —
    it was previously shown only for ``hop`` / ``direct``, so two ``scratch``
    atoms on different paths rendered byte-identically (2026-07-30 review, F6).

    Shown UNCONDITIONALLY when present, even where it merely repeats the entry's
    own host: a redundant token is cheap, and a conditional that decides when the
    target "adds nothing" is exactly the kind of rule that later hides a
    difference. Only an unfed placeholder — which has no target — omits it.
    """
    sensor = str(atom.get("sensor") or "?")
    route = str(atom.get("route") or "")
    base = f"{sensor}/{route}" if route and route != "n/a" else sensor
    target = str(atom.get("target") or "")
    return f"{base} → {target}" if target else base


def _atom_line(atom: dict[str, Any]) -> str:
    """One atom row: subject, verdict, age, staleness, latency, provenance."""
    subject = _subject(atom)
    verdict = str(atom.get("verdict") or "unknown")
    if verdict == "unknown" and atom.get("at") is None:
        return f"  - {subject}: unknown ({_UNKNOWN_REASON})"
    parts = [_age_phrase(atom.get("age_seconds"))]
    if atom.get("stale"):
        horizon = atom.get("stale_after_seconds")
        parts.append(f"STALE (horizon {int(horizon)}s)" if horizon is not None else "STALE")
    latency = atom.get("latency_ms")
    if latency is not None:
        parts.append(f"{int(latency)}ms")
    source = atom.get("source")
    if source:
        parts.append(f"via {source}")
    detail = atom.get("detail")
    if detail:
        parts.append(str(detail))
    return f"  - {subject}: {verdict} ({' · '.join(parts)})"


def _counts_line(counts: dict[str, int]) -> str:
    """``ready 1 · stale 2 · degraded 0 · unknown 1`` in the fixed order."""
    order = ("ready", "stale", "degraded", "unknown")
    return " · ".join(f"{name} {int(counts.get(name, 0))}" for name in order)


def render_readiness(
    entries: Sequence[dict[str, Any]],
    *,
    computed_at: str,
    counts: dict[str, int],
) -> str:
    """Compose the readiness digest for *entries* as of *computed_at*.

    *entries* are plain dicts in the already-decided order, each carrying
    ``cluster`` / ``host`` / ``verdict`` / ``atoms`` / ``ledger_corrupt`` — the
    same keys the wire model declares, passed as dicts so this module stays
    ``_wire``-free.
    """
    lines: list[str] = [
        f"cluster readiness · computed {computed_at} · re-run for current state",
        "",
        _counts_line(counts),
        "",
    ]
    if not entries:
        lines.append("(no clusters configured and no readiness ledger on this machine)")
        return "\n".join(lines).rstrip() + "\n"

    for entry in entries:
        cluster = entry.get("cluster")
        host = str(entry.get("host") or "?")
        title = f"{cluster} ({host})" if cluster else host
        verdict = str(entry.get("verdict") or "unknown")
        corrupt = bool(entry.get("ledger_corrupt"))
        # A corrupt ledger IS 'unknown', but not for the default reason — say
        # which, in the headline, so the entry does not open with a reassuring
        # claim it contradicts two lines later (F7).
        if corrupt and verdict == "unknown":
            gloss = _CORRUPT_GLOSS
        else:
            gloss = _VERDICT_GLOSS.get(verdict, "")
        lines.append(f"- {title}: {verdict}" + (f" — {gloss}" if gloss else ""))
        if corrupt:
            reason = str(entry.get("ledger_corruption_reason") or "")
            because = f": {reason}" if reason else ""
            lines.append(
                f"  - ledger file could not be read{because}; reporting an EMPTY ledger "
                "(the next observation rewrites it)"
            )
        last = entry.get("ledger_last_corruption")
        if isinstance(last, dict) and not corrupt:
            # A rebuild discarded a corrupt file at some point. The atoms below
            # are real, but the operator should learn ONCE that something was
            # lost rather than have it vanish with the file.
            when = str(last.get("at") or "?")
            why = str(last.get("reason") or "unknown reason")
            lines.append(
                f"  - note: a previous ledger file was discarded as unreadable "
                f"at {when} ({why}); atoms below were recorded after that"
            )
        atoms = entry.get("atoms")
        for atom in atoms if isinstance(atoms, list) else []:
            if isinstance(atom, dict):
                lines.append(_atom_line(atom))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
