"""Percent-format section model — the source-of-truth parser for the
notebook-audit substrate (Wave A / T1, see ``docs/design/notebook-audit.md``).

The LLM drafts RAW PYTHON in jupytext *percent* format (``# %%`` cells); the
notebook is only ever a caller-side render (D-source). This module is the ONLY
reader of that ``.py`` and is deliberately jupytext-illiterate: it needs cell
BOUNDARIES and nothing about jupytext's cell-metadata grammar. Two structural
concepts, both plain comments:

* **Cell delimiter** — a col-0 line beginning ``# %%`` opens a new cell. The
  ``# %% [markdown]`` / ``# %% [md]`` variants are accepted OPAQUELY: only the
  boundary matters, never the metadata after it (Q1 boundary-drift flag: marker
  syntax stays comment-only, so core never learns jupytext's grammar).
* **Section marker** — a col-0 ``# hpc-audit-section: <slug>`` comment
  (deliberately NOT jupytext metadata syntax) recognized ONLY when it is the
  first non-blank line INSIDE a cell (i.e. the first non-blank body line after
  that cell's ``# %%`` delimiter). A section = the marker cell plus every
  following cell until the next marker cell or EOF.

Hashing is over a **minimal, deterministic normalization** (see
:func:`normalize_source`) so a file authored on Windows (CRLF) and the same file
on a POSIX box (LF) hash identically:

    1. newlines unified — ``\\r\\n`` and lone ``\\r`` → ``\\n``;
    2. trailing whitespace stripped per line.

Nothing else is touched (no case-fold, no blank-line collapse, no form-feed
handling) — normalization must not erase a real edit. ``section_sha`` is the
sha256 of a section's normalized source; ``module_sha`` is the sha256 of the
WHOLE normalized module (preamble included — see the preamble note below), so an
edit ANYWHERE moves ``module_sha`` while a section's hash moves only when that
section's own content changes.

**Preamble choice (recorded per the T1 brief):** content before the first
section marker belongs to NO section but IS covered by ``module_sha`` (the
module hash is over the whole file). This keeps ``module_sha`` a true fingerprint
of the entire source while sections stay the unit of sign-off.

Templates are parsed by the SAME :func:`parse_percent_source` — there is no
separate template parser (a template and a source that share a section's
content share that section's ``section_sha`` by construction).

Slug shape reuses the state layer's one filesystem-safe slug pattern
(``state.runs._RUN_ID_RE`` == ``^[A-Za-z0-9._\\-]+$`` — the same class scope
tags and run ids pin), never a fresh regex. Pure, stdlib-only (``hashlib`` +
``re``): no jupytext, no I/O, no ``_wire`` import.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from hpc_agent import errors
from hpc_agent.state.runs import _RUN_ID_RE

__all__ = [
    "Section",
    "ParsedModule",
    "normalize_source",
    "sha256_normalized",
    "parse_percent_source",
]

#: A jupytext percent-format CELL delimiter — any col-0 line beginning with this
#: prefix opens a new cell. Matched opaquely; the metadata after it is ignored.
_CELL_DELIM_PREFIX = "# %%"

#: The section marker: a col-0 ``# hpc-audit-section: <slug>`` comment. The slug
#: is captured greedily and validated against :data:`_RUN_ID_RE`.
_MARKER_RE = re.compile(r"^#\s*hpc-audit-section:\s*(?P<slug>.*)$")


@dataclass(frozen=True)
class Section:
    """One audited section: the marker cell plus following cells to the next
    marker (or EOF).

    * ``slug`` — the caller-authored section id (validated slug shape).
    * ``source`` — the section's raw source (newlines unified to ``\\n``,
      otherwise verbatim) for diff-from-template (T5 diffs this).
    * ``section_sha`` — sha256 over the NORMALIZED ``source`` (the un-fakeable
      hash the sign-off gate recomputes).
    * ``start_line`` — 0-based index of the section's first line in the unified
      module (its ``# %%`` delimiter, or line 0 for a leading marker); for
      stable ordering / diagnostics, never hashed.
    """

    slug: str
    source: str
    section_sha: str
    start_line: int


@dataclass(frozen=True)
class ParsedModule:
    """The parsed percent-format module.

    * ``module_sha`` — sha256 over the whole normalized module (preamble
      included).
    * ``sections`` — sections in source order.
    * ``preamble`` — raw source before the first section marker (may be ``""``);
      belongs to no section but is covered by ``module_sha``.
    """

    module_sha: str
    sections: tuple[Section, ...]
    preamble: str

    @property
    def slugs(self) -> tuple[str, ...]:
        """Section slugs in source order (T4 checks the template's slugs are an
        order-preserving subsequence of these)."""
        return tuple(s.slug for s in self.sections)


def normalize_source(text: str) -> str:
    """Return *text* under the minimal, deterministic hashing normalization.

    Unify newlines (``\\r\\n`` and lone ``\\r`` → ``\\n``) then strip trailing
    whitespace per line. Cross-platform stable: a CRLF file and its LF twin
    normalize identically. Nothing else is altered.
    """
    unified = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in unified.split("\n"))


def sha256_normalized(text: str) -> str:
    """sha256 hexdigest of :func:`normalize_source` of *text* (utf-8).

    The one hashing primitive: ``section_sha``, ``module_sha``, and the
    ``linked_sources`` module hashes (T4) all route through it so every recompute
    site agrees byte-for-byte.
    """
    return hashlib.sha256(normalize_source(text).encode("utf-8")).hexdigest()


def _unified_lines(text: str) -> list[str]:
    """Newline-unified line list (no line endings), the segmentation substrate."""
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _extract_slug(line: str) -> str:
    """Return the validated slug from a marker *line*.

    Raises :class:`errors.SpecInvalid` on a missing or non-slug value — a
    marker with a malformed slug is loud, never silently skipped.
    """
    m = _MARKER_RE.match(line)
    slug = (m.group("slug") if m else "").strip()
    if not slug:
        raise errors.SpecInvalid(f"hpc-audit-section marker missing a slug: {line!r}")
    if not _RUN_ID_RE.fullmatch(slug):
        raise errors.SpecInvalid(
            f"invalid hpc-audit-section slug {slug!r} (must match ^[A-Za-z0-9._-]+$)"
        )
    return slug


def parse_percent_source(text: str) -> ParsedModule:
    """Parse a jupytext percent-format ``.py`` into its section model.

    Segments on ``# %%`` cell delimiters, recognizes ``# hpc-audit-section:``
    markers only at a cell's first-non-blank body line, and computes
    ``section_sha`` / ``module_sha`` over the normalized source.

    Raises :class:`errors.SpecInvalid` (the malformed-spec class the taxonomy
    already carries — no new error_code, which would be a breaking envelope
    change) on:

    * an invalid or missing slug on a marker;
    * a duplicate slug;
    * a MISPLACED marker — a col-0 ``# hpc-audit-section:`` comment that is NOT
      its cell's first non-blank body line. Silently ignoring it would drop a
      section the author intended (the silent-failure class the repo forbids),
      so it is loud. (An INDENTED ``# hpc-audit-section:`` comment is not a
      marker at all — it is ordinary in-body content — and is left untouched.)
    """
    lines = _unified_lines(text)
    n = len(lines)

    # Cell starts: every ``# %%`` delimiter line, plus line 0 for the leading
    # implicit cell (content before the first delimiter, e.g. a module docstring).
    delim_idxs = [i for i, line in enumerate(lines) if line.startswith(_CELL_DELIM_PREFIX)]
    cell_starts = sorted({0, *delim_idxs})

    # Every col-0 marker-shaped line in the whole module. Each MUST turn out to
    # be some cell's first non-blank body line, else it is misplaced.
    all_marker_idxs = [i for i, line in enumerate(lines) if _MARKER_RE.match(line)]

    section_starts: list[tuple[int, str]] = []  # (cell_start_line, slug), in order
    valid_marker_idxs: set[int] = set()
    for k, cs in enumerate(cell_starts):
        ce = cell_starts[k + 1] if k + 1 < len(cell_starts) else n
        # Body begins after the delimiter line (or at cs for the leading cell).
        body_start = cs + 1 if lines[cs].startswith(_CELL_DELIM_PREFIX) else cs
        first_nonblank = next(
            (i for i in range(body_start, ce) if lines[i].strip()),
            None,
        )
        if first_nonblank is not None and _MARKER_RE.match(lines[first_nonblank]):
            slug = _extract_slug(lines[first_nonblank])  # raises on a bad slug
            section_starts.append((cs, slug))
            valid_marker_idxs.add(first_nonblank)

    misplaced = [i for i in all_marker_idxs if i not in valid_marker_idxs]
    if misplaced:
        raise errors.SpecInvalid(
            "hpc-audit-section marker must be the first non-blank line of its "
            f"cell; found a misplaced marker at line {misplaced[0] + 1}: "
            f"{lines[misplaced[0]]!r}"
        )

    seen: set[str] = set()
    for _cs, slug in section_starts:
        if slug in seen:
            raise errors.SpecInvalid(f"duplicate hpc-audit-section slug: {slug!r}")
        seen.add(slug)

    sections: list[Section] = []
    for j, (cs, slug) in enumerate(section_starts):
        span_end = section_starts[j + 1][0] if j + 1 < len(section_starts) else n
        source = "\n".join(lines[cs:span_end])
        sections.append(
            Section(
                slug=slug,
                source=source,
                section_sha=sha256_normalized(source),
                start_line=cs,
            )
        )

    first_cs = section_starts[0][0] if section_starts else n
    preamble = "\n".join(lines[:first_cs])

    return ParsedModule(
        module_sha=sha256_normalized(text),
        sections=tuple(sections),
        preamble=preamble,
    )
