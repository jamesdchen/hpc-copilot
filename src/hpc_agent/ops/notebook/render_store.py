"""The TRUSTED-DISPLAY render store — content-addressed section render files.

Design origin: ``docs/design/notebook-audit.md`` (the v1.5 trusted-display lock,
user-approved 2026-07-07 — "prose recruits, gates guarantee"). The audit view an
agent relays into chat is MODEL-CARRIED and unforceable; the trusted artifact is
a CONTENT-ADDRESSED RENDER FILE written by CODE. This module owns that file: the
code-written path, the machine-parseable header block, and the fail-soft parse
the T8 sign-off gate reads back.

The load-bearing property lives in the gate (``ops/decision/journal.py``'s
``_assert_signoff_authorship``), NOT here: a sign-off may not land unless the
render file addressed by the resolved ``view_sha`` exists on disk and was
produced against CURRENT source (its header ``section_sha`` equals the gate's
freshly-recomputed section sha). This module only writes and reads; it enforces
nothing. Same trust model as every store: the filesystem is code-written, so
tool-surface enforcement is the guarantee and filesystem forgery is out of scope
(the ``journal.py`` honest-limit paragraph).

Path scheme (content-addressed by the per-section ``view_sha``)::

    <experiment>/.hpc/renders/<audit_id>/<slug>.<view_sha12>.md

Each file OPENS with a header block of machine-parseable HTML-comment lines —
invisible in a rendered markdown view but exactly recoverable by
:func:`read_render_header` — carrying ``{audit_id, section, section_sha,
view_sha}``, followed by a blank line and the code-rendered markdown projection
of the section (the same deterministic ``_render_section`` the whole-view
markdown uses). Bytes are DETERMINISTIC: no timestamps, no absolute paths — the
same section view yields a byte-identical file, so a re-render is a no-op and the
content address is stable across platforms.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from hpc_agent._kernel.contract.layout import RepoLayout
from hpc_agent.ops.notebook.audit_view import _render_section

if TYPE_CHECKING:
    from hpc_agent.ops.notebook.audit_view import SectionView

__all__ = [
    "HEADER_KEYS",
    "RenderDigest",
    "write_render",
    "render_path",
    "read_render_header",
    "read_render_digest",
]

#: The header keys every render file carries — the gate cross-checks all four.
HEADER_KEYS: tuple[str, ...] = ("audit_id", "section", "section_sha", "view_sha")

#: One header line: ``<!-- hpc-render <key>: <value> -->``. HTML comments so the
#: block is invisible in a rendered markdown view yet exactly machine-parseable.
_HEADER_PREFIX = "hpc-render"
_HEADER_LINE_RE = re.compile(
    r"^<!--\s*" + re.escape(_HEADER_PREFIX) + r"\s+(?P<key>[a-z_]+):\s*(?P<value>.*?)\s*-->$"
)

#: How many chars of the ``view_sha`` name the file (the content address). 12 is
#: the ``JournalLayout.repo_hash`` precedent — collision-safe for a section pool.
_VIEW_SHA_ADDRESS_LEN = 12


def _renders_root(experiment_dir: Path, audit_id: str) -> Path:
    """``<experiment>/.hpc/renders/<audit_id>/`` (not created)."""
    return RepoLayout(experiment_dir).hpc / "renders" / audit_id


def render_path(experiment_dir: Path, *, audit_id: str, section: str, view_sha: str) -> Path:
    """The content-addressed path a section's render file lives at.

    Addressed by the per-section ``view_sha`` (its first
    :data:`_VIEW_SHA_ADDRESS_LEN` chars) so the file the sign-off gate looks up is
    keyed on exactly what the human was shown. Pure — creates nothing.
    """
    name = f"{section}.{view_sha[:_VIEW_SHA_ADDRESS_LEN]}.md"
    return _renders_root(experiment_dir, audit_id) / name


def _render_bytes(*, audit_id: str, view: SectionView) -> str:
    """The deterministic file body: the header block + the section markdown.

    No timestamps, no absolute paths — same inputs → byte-identical output.
    """
    header = [
        f"<!-- {_HEADER_PREFIX} audit_id: {audit_id} -->",
        f"<!-- {_HEADER_PREFIX} section: {view.slug} -->",
        f"<!-- {_HEADER_PREFIX} section_sha: {view.section_sha} -->",
        f"<!-- {_HEADER_PREFIX} view_sha: {view.view_sha} -->",
        "",
    ]
    body = _render_section(view)
    return "\n".join([*header, *body]).rstrip() + "\n"


def write_render(experiment_dir: Path, *, audit_id: str, view: SectionView) -> Path:
    """Write *view*'s content-addressed render file and return its path.

    Creates the ``.hpc/renders/<audit_id>/`` parent lazily (the ``RepoLayout``
    idiom) and writes the header + markdown at the ``view_sha``-addressed path.
    The write is idempotent by construction: the bytes are deterministic and the
    path is content-addressed, so re-rendering the same section view rewrites the
    same file with the same content.
    """
    path = render_path(experiment_dir, audit_id=audit_id, section=view.slug, view_sha=view.view_sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_bytes(audit_id=audit_id, view=view), encoding="utf-8")
    return path


def _parse_header(text: str) -> dict[str, str] | None:
    """Parse a render file's leading header block from *text*, or ``None``.

    Reads the leading run of ``<!-- hpc-render <key>: <value> -->`` comment lines
    (blank lines tolerated) and stops at the first markdown body line. Returns the
    mapping only when ALL of :data:`HEADER_KEYS` are present. The single header
    grammar both :func:`read_render_header` and :func:`read_render_digest` share.
    """
    header: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue  # blank line inside/after the header block
        m = _HEADER_LINE_RE.match(stripped)
        if m is None:
            break  # first non-header line — the markdown body starts here
        header[m.group("key")] = m.group("value")
    if not all(key in header for key in HEADER_KEYS):
        return None
    return header


def read_render_header(path: Path) -> dict[str, str] | None:
    """Parse a render file's header block, or ``None`` (fail-soft).

    Reads the leading run of ``<!-- hpc-render <key>: <value> -->`` comment lines
    (blank lines tolerated) and stops at the first markdown body line. Returns the
    mapping only when ALL of :data:`HEADER_KEYS` are present; any missing key, an
    unreadable file, or a header-less file reads ``None`` — a soft absence the gate
    turns into a loud, path-naming refusal (a malformed header must never read as a
    valid trusted display).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return _parse_header(text)


# --- the bounded audit-view digest (E-render) -------------------------------
# ``docs/design/mcp-elicitation.md`` E-render (SHIPPED 2026-07-09): the sign-off
# elicitation popup carries a CODE-COMPUTED digest of the trusted render — diff
# stats, assert table, lint-flag count + the ``view_sha12`` — while the full
# render stays on disk for the Read pane (RULING 1: digest, not full render).
# The digest is derived from the ON-DISK render file (the code-authored trusted
# artifact the T8 gate binds), NEVER re-derived from the notebook ``.py`` source
# — same input the human signed against. It is BOUNDED by construction: counts
# plus a capped, per-item-truncated assertion list, never the diff body and
# never an unbounded source echo.

#: How many declared assertions the digest lists verbatim before eliding.
_DIGEST_MAX_ASSERTIONS = 6
#: The per-assertion character cap in the digest (each entry truncated to this).
_DIGEST_MAX_ASSERTION_CHARS = 120


@dataclass(frozen=True)
class RenderDigest:
    """A bounded, code-computed digest of ONE section's on-disk render (E-render).

    Every field is derived from the render file's own header + code-rendered body
    (the same three audit-view projections — diff-from-template, assertion table,
    lint flags — plus the identifiers), never from the notebook source. ``view_sha``
    / ``section`` / ``section_sha`` / ``audit_id`` come from the header;
    ``classification`` and the counts come from the body. :attr:`assertions` is
    capped at :data:`_DIGEST_MAX_ASSERTIONS` entries each truncated to
    :data:`_DIGEST_MAX_ASSERTION_CHARS`; :attr:`assertion_count` is the full count,
    so a caller can disclose how many were elided.
    """

    audit_id: str
    section: str
    section_sha: str
    view_sha: str
    classification: str
    diff_added: int
    diff_removed: int
    assertion_count: int
    assertions: tuple[str, ...]
    lint_flag_count: int


def _parse_body_digest(text: str) -> tuple[str, int, int, int, tuple[str, ...]]:
    """Scan a render body for its digest fields (pure, fail-soft on shape drift).

    Returns ``(classification, diff_added, diff_removed, assertion_count,
    assertions)``. Anchored on the stable ``_render_section`` sub-headers
    (``### diff-from-template`` / ``### assertions`` / ``### lint flags``); a
    render body carries exactly ONE section, so there is no cross-section
    ambiguity. Diff stats count added/removed lines INSIDE the ```` ```diff ````
    fence only (never the ``+++``/``---`` file labels); assertion entries are the
    ``- L<n>: …`` lines; each is truncated for the bound.
    """
    classification = ""
    diff_added = diff_removed = 0
    assertions: list[str] = []
    section: str | None = None
    in_diff_fence = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- classification:"):
            classification = stripped.split(":", 1)[1].strip()
            continue
        if stripped == "### diff-from-template":
            section, in_diff_fence = "diff", False
            continue
        if stripped == "### assertions":
            section = "assertions"
            continue
        if stripped == "### lint flags":
            section = "lint"
            continue
        if section == "diff":
            if stripped.startswith("```"):
                in_diff_fence = not in_diff_fence
                continue
            if in_diff_fence:
                if line.startswith("+") and not line.startswith("+++"):
                    diff_added += 1
                elif line.startswith("-") and not line.startswith("---"):
                    diff_removed += 1
        elif section == "assertions" and stripped.startswith("- L"):
            entry = stripped[2:]
            if len(entry) > _DIGEST_MAX_ASSERTION_CHARS:
                entry = entry[: _DIGEST_MAX_ASSERTION_CHARS - 1] + "…"
            if len(assertions) < _DIGEST_MAX_ASSERTIONS:
                assertions.append(entry)
    assertion_count = _count_assertion_lines(text)
    return classification, diff_added, diff_removed, assertion_count, tuple(assertions)


def _count_assertion_lines(text: str) -> int:
    """The TOTAL count of ``- L…`` assertion lines in the assertions block."""
    count = 0
    in_assertions = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "### assertions":
            in_assertions = True
            continue
        if stripped.startswith("### ") or stripped.startswith("## "):
            in_assertions = False
            continue
        if in_assertions and stripped.startswith("- L"):
            count += 1
    return count


def _count_lint_flag_lines(text: str) -> int:
    """The count of ``- …`` lint-flag lines in the lint-flags block (``(none)``
    is rendered WITHOUT a leading ``- `` and so counts as zero)."""
    count = 0
    in_lint = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "### lint flags":
            in_lint = True
            continue
        if stripped.startswith("### ") or stripped.startswith("## "):
            in_lint = False
            continue
        if in_lint and stripped.startswith("- "):
            count += 1
    return count


def read_render_digest(path: Path) -> RenderDigest | None:
    """Read a section render off disk and compute its bounded digest, or ``None``.

    Fail-soft exactly like :func:`read_render_header`: an absent/unreadable file
    or an unparseable header reads ``None`` (the caller discloses a reason and
    degrades — never a crash, never an unmarked silent fallback). Reads the file
    ONCE; the digest is over the code-authored render bytes only.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    header = _parse_header(text)
    if header is None:
        return None
    classification, diff_added, diff_removed, assertion_count, assertions = _parse_body_digest(text)
    return RenderDigest(
        audit_id=header["audit_id"],
        section=header["section"],
        section_sha=header["section_sha"],
        view_sha=header["view_sha"],
        classification=classification,
        diff_added=diff_added,
        diff_removed=diff_removed,
        assertion_count=assertion_count,
        assertions=assertions,
        lint_flag_count=_count_lint_flag_lines(text),
    )
