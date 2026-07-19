"""Module-attention items — the ONE attention charge per UNSIGNED src module.

Wave-3 piece 3 ("src modules as signable attention units"). An audited section
that imports a src module under a ``source_root`` is a DEPENDENT of that module.
When the module's CURRENT content is UNSIGNED (no human module sign-off of its
current sha), the human's attention is owed ONCE — on the MODULE — not once per
dependent section (the per-dependent noise the maintainer ruled against:
"attention charged per CHANGED PIECE, never per dependent"). This builder resolves
the audit's linked modules, dedups by file, and emits ONE
:class:`ModuleAttentionItem` per UNSIGNED module, listing its dependents.

Piece 5 (moved-code disclosure) rides the same item, ADVISORY ONLY: a best-effort
normalized line-overlap match of the module body against the audit's already
HUMAN-signed SECTION bodies — surfacing "this src module is code you already
signed as a section" (the extraction the recurrence nudge predicts). The fuzzy
match NEVER clears anything; it is a disclosure the human reads, nothing more.

Pure of trust: reads the ``.py`` files + the journals, computes hashes and
overlaps, and returns advisory data. It clears nothing and revokes nothing — the
graduation gate's linked-source drift check (``ops/notebook_gate.py``) is the seat
that actually treats a SIGNED module as current.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from hpc_agent.ops.notebook.linked_sources import LinkedEngine, resolve_section_engines
from hpc_agent.state import notebook_audit
from hpc_agent.state.audit_source import ParsedModule, normalize_source, sha256_normalized

__all__ = ["ModuleAttentionItem", "build_module_attention"]

#: Minimum fraction of a module's normalized non-blank lines that must appear in a
#: signed section body before the moved-code disclosure fires (bounded, advisory).
_MOVED_MIN_OVERLAP = 0.5
#: Floor on absolute matching lines — two-line coincidences are not "moved code".
_MOVED_MIN_LINES = 3


@dataclass(frozen=True)
class ModuleAttentionItem:
    """ONE attention charge for an UNSIGNED linked module (never per dependent).

    * ``module`` — a representative import display name (``M`` / ``M.f``).
    * ``file`` — the module's experiment-relative POSIX path.
    * ``module_sha12`` — the first 12 chars of the module's current normalized sha.
    * ``dependents`` — the section slugs importing this module (why it matters).
    * ``last_signed_sha12`` — the sha12 of the module's most-recent HUMAN sign-off
      at a DIFFERENT sha (the "diff vs last-signed" anchor), or ``None`` when the
      module was never signed before.
    * ``moved_from_section`` / ``moved_overlap`` — piece 5 advisory: a HUMAN-signed
      section whose body this module's content closely matches, and ``(matching,
      total)`` normalized-line counts. ``None`` when nothing matches. NEVER a
      clearing input.
    """

    module: str
    file: str
    module_sha12: str
    dependents: tuple[str, ...]
    last_signed_sha12: str | None = None
    moved_from_section: str | None = None
    moved_overlap: tuple[int, int] | None = None


def _normalized_line_set(source: str) -> set[str]:
    """The set of NON-BLANK normalized lines of *source* (the overlap unit)."""
    return {ln for ln in normalize_source(source).split("\n") if ln.strip()}


def _moved_match(
    module_lines: set[str], signed_bodies: dict[str, str]
) -> tuple[str | None, tuple[int, int] | None]:
    """Best signed-section overlap for a module body, or ``(None, None)``.

    Compares the module's normalized non-blank line set against each HUMAN-signed
    section body's; the best section by fraction-of-module-lines-matched wins when
    it clears both :data:`_MOVED_MIN_OVERLAP` and :data:`_MOVED_MIN_LINES`. Cheap,
    bounded, and ADVISORY — a fuzzy match never clears anything (the pin).
    """
    if not module_lines:
        return None, None
    best_slug: str | None = None
    best_matched = 0
    for slug, body in signed_bodies.items():
        matched = len(module_lines & _normalized_line_set(body))
        if matched > best_matched:
            best_matched, best_slug = matched, slug
    total = len(module_lines)
    if best_slug is None or best_matched < _MOVED_MIN_LINES:
        return None, None
    if best_matched / total < _MOVED_MIN_OVERLAP:
        return None, None
    return best_slug, (best_matched, total)


def build_module_attention(
    experiment_dir: Path,
    *,
    source: ParsedModule,
    source_roots: list[str],
    signed_section_bodies: dict[str, str],
) -> list[ModuleAttentionItem]:
    """The UNSIGNED linked modules of an audited *source*, ONE item each.

    Resolves every section's imports to engine files under *source_roots* (the ONE
    resolver, :func:`resolve_section_engines`), dedups by file, and — for each
    module whose CURRENT normalized sha carries NO human module sign-off
    (:func:`~hpc_agent.state.notebook_audit.module_sha_signed`) — emits one
    :class:`ModuleAttentionItem` naming its dependents, the last-signed sha to diff
    against, and (piece 5) any HUMAN-signed section body it matches. A SIGNED module
    produces nothing — its dependents cost no attention. No roots → no items (the
    fail-open default).

    *signed_section_bodies* is ``{slug: section_source}`` for the audit's
    HUMAN-signed sections (``signed_current`` / ``reused``) — the corpus the
    moved-code disclosure matches against. Deterministic: items are ordered by
    module file path.
    """
    root_dirs = [(Path(r) if Path(r).is_absolute() else experiment_dir / r) for r in source_roots]
    if not root_dirs:
        return []

    # file → (representative engine, dependent slugs). Resolve per section so a
    # module imported by several sections collects ALL its dependents (one item).
    by_file: dict[str, LinkedEngine] = {}
    dependents: dict[str, set[str]] = {}
    for section in source.sections:
        for eng in resolve_section_engines(section.source, experiment_dir, root_dirs):
            by_file.setdefault(eng.file, eng)
            dependents.setdefault(eng.file, set()).add(section.slug)

    items: list[ModuleAttentionItem] = []
    for file in sorted(by_file):
        eng = by_file[file]
        path = Path(file)
        if not path.is_absolute():
            path = experiment_dir / path
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # unreadable now — nothing to charge attention on
        current_sha = sha256_normalized(text)
        if notebook_audit.module_sha_signed(experiment_dir, current_sha):
            continue  # signed at its current sha → no attention owed (the exemption)
        # Module IDENTITY is the FILE relpath — a module sign-off records
        # ``resolved['module']`` as the readable relpath its gate recomputes the sha
        # from (never the import DISPLAY name), so the last-signed lookup keys on it.
        last = notebook_audit.last_module_signoff(experiment_dir, file)
        last_sha12 = (
            last.content_sha[:12] if last is not None and last.content_sha != current_sha else None
        )
        moved_slug, overlap = _moved_match(_normalized_line_set(text), signed_section_bodies)
        items.append(
            ModuleAttentionItem(
                module=eng.module,
                file=file,
                module_sha12=current_sha[:12],
                dependents=tuple(sorted(dependents.get(file, set()))),
                last_signed_sha12=last_sha12,
                moved_from_section=moved_slug,
                moved_overlap=overlap,
            )
        )
    return items
