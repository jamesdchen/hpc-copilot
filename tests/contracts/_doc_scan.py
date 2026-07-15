"""Shared masking/scoping seam for the operational-doc contract pins.

Two contract tests scan the operational-truth doc surfaces
(``docs/internals`` + ``docs/workflows``) for silent rot:
:mod:`tests.contracts.test_doc_references` (console-script + module-path
references that a refactor has since broken) and
:mod:`tests.contracts.test_doc_frozen_counts` (frozen ``N primitives`` /
``N verbs`` / … literals the registry has outgrown). Both need the SAME
scope definition and the SAME masking rules, so the one-definition seam
lives here and both import it — rather than each copy-pasting the fence /
drift-log maskers (a duplication the engineering-principles "one
definition, named tests" rule warns against).

The two masking exclusions keep every in-scope pin honest rather than
noisy:

* **Fenced code blocks** (```` ``` ````-delimited) are masked. They carry
  worked examples and shell transcripts whose tokens/counts are
  illustrative, not load-bearing operational claims.
* **Drift-log sections** (a heading whose text contains "drift log", up to
  the next same-or-shallower heading) are masked. Their whole purpose is to
  record paths/scripts/counts that USED to exist — scanning them would
  fault the very honesty they provide.

Both exclusions preserve line numbers (masked regions become blank lines)
so failure messages still point at the right line.

Stdlib only (``re``, ``pathlib``) so a pin can stay import-light if it
wants; :func:`_scope_docs` anchors on :data:`tests._paths.REPO_ROOT`.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests._paths import REPO_ROOT

# Operational-truth doc surfaces. Design/plans narrate history — out of
# scope (architect memo §6). Both doc pins share this exact tuple.
SCOPE_DIRS: tuple[str, ...] = ("docs/internals", "docs/workflows")


_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_HEADING_RE = re.compile(r"^(#+)\s+(.*)$")


def _blank_like(match: re.Match[str]) -> str:
    """Replace a matched region with as many newlines as it spanned."""
    return "\n" * match.group(0).count("\n")


def _mask_drift_log_sections(text: str) -> str:
    """Blank out any ``# ... drift log ...`` section, up to the next
    same-or-shallower heading, preserving line count."""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    skip_depth: int | None = None
    for line in lines:
        m = _HEADING_RE.match(line.rstrip("\n"))
        if m:
            depth = len(m.group(1))
            if skip_depth is not None and depth <= skip_depth:
                skip_depth = None  # section ended; fall through to emit
            if skip_depth is None and "drift log" in m.group(2).lower():
                skip_depth = depth
                out.append("\n" if line.endswith("\n") else "")
                continue
        if skip_depth is not None:
            out.append("\n" if line.endswith("\n") else "")
        else:
            out.append(line)
    return "".join(out)


def _mask(text: str) -> str:
    """Fenced blocks + drift-log sections blanked; line numbers preserved."""
    return _mask_drift_log_sections(_FENCE_RE.sub(_blank_like, text))


def _scope_docs() -> list[Path]:
    """Every ``*.md`` under the in-scope operational-truth dirs, sorted."""
    out: list[Path] = []
    for rel in SCOPE_DIRS:
        d = REPO_ROOT / rel
        if d.is_dir():
            out.extend(p for p in sorted(d.rglob("*.md")) if p.is_file())
    return out


def _line_of(text: str, offset: int) -> int:
    """1-based line number of *offset* within *text*."""
    return text.count("\n", 0, offset) + 1
