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
from pathlib import Path
from typing import TYPE_CHECKING

from hpc_agent._kernel.contract.layout import RepoLayout
from hpc_agent.ops.notebook.audit_view import _render_section

if TYPE_CHECKING:
    from hpc_agent.ops.notebook.audit_view import SectionView

__all__ = [
    "HEADER_KEYS",
    "write_render",
    "render_path",
    "read_render_header",
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
