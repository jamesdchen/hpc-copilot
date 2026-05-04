"""Shared helpers for the build_*_index.py scripts.

Both ``scripts/build_operations_index.py`` and
``scripts/build_primitive_index.py`` group primitives by verb and
render side-effects cells. This module consolidates:

- :data:`REPO_ROOT` — repo top, parent of ``scripts/``.
- :data:`VERB_ORDER` — canonical verb-section order.
- :func:`sort_verbs` — verb ordering with unknown verbs falling
  alphabetically to the end.
- :func:`summarize_side_effects` — compact one-line rendering of a
  primitive's ``side_effects`` list, including structured ``{verb:
  target}`` entries (the operations-index renderer used to silently
  drop the structured form).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Canonical order of primitive verb sections. Verbs not in this list
# fall to the end alphabetically (see :func:`sort_verbs`).
VERB_ORDER: list[str] = ["query", "validate", "mutate", "submit", "scaffold", "workflow"]


def sort_verbs(verbs: list[str]) -> list[str]:
    """Return *verbs* sorted by ``VERB_ORDER`` then alphabetically.

    Unknown verbs (those absent from ``VERB_ORDER``) sort to the end in
    alphabetical order.
    """
    return sorted(
        verbs,
        key=lambda v: (VERB_ORDER.index(v) if v in VERB_ORDER else len(VERB_ORDER), v),
    )


def summarize_side_effects(side_effects: list) -> str:
    """Render a primitive's ``side_effects`` list to one cell.

    Accepts a mixed list of strings and ``{verb: target}`` dicts (the
    YAML frontmatter form used in ``docs/primitives/*.md``). Structured
    entries are rendered as ``verb: \\`target\\``` so a single line
    captures both shapes.

    The historic operations-index inline renderer dropped structured
    entries silently — using this function fixes that quietly.
    """
    if not side_effects:
        return "_none_"
    parts: list[str] = []
    for entry in side_effects:
        if isinstance(entry, dict):
            for verb, target in entry.items():
                short_target = target.split(" ")[0].rstrip(",.;")
                parts.append(f"{verb}: `{short_target}`")
        else:
            parts.append(str(entry))
    return "; ".join(parts)
