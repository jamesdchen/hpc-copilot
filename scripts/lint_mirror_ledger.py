r"""CI lint: every "mirror" comment names its twin and its pinning test.

Sibling to ``lint_schema_versions.py`` / ``lint_telemetry_labels.py``. Those
mechanize a *specific* duplicated constant; this one mechanizes the **prose
convention** that pervades the tree — a comment saying a block is "kept in
sync" / "in lock-step" / "mirrors X" is a promise that some *other* place holds
a twin and that *something* fails if the two drift. Today that promise is
free-text and unchecked (the N7 finding): a reader cannot tell, from
``# … mirrors state/evidence.py::CITATION_KINDS``, WHERE the twin lives or WHAT
would catch a drift. This lint turns the convention into a checked contract.

Per the determinism principle (``docs/internals/engineering-principles.md``): a
rule the code relies on is enforced, not merely documented. "These two stay in
sync" is exactly such a rule.

The contract
------------

Every *mirror comment* — a comment or docstring/string line containing one of
the trigger phrases (case-insensitive):

    * ``kept in sync``
    * ``lock-step`` / ``lockstep``
    * ``mirrors`` (as a word)

must, in a file that is NOT deferred (see allowlist), be accompanied by a
**structured MIRROR annotation** comment within a few lines::

    # MIRROR: <twin> pinned-by <test>

where ``<twin>`` names the sibling location (``path::symbol`` or a prose
locator) and ``<test>`` names the test / lint that fails when the two drift.
Both halves must be non-empty. Example (``cli/skill_returns.py``)::

    # Should never happen — _KNOWN_SKILLS and the schemas dir must stay
    # in lock-step (tests/cli/test_skill_returns.py pins this).
    # MIRROR: hpc_agent.cli.skill_returns::_KNOWN_SKILLS <-> schemas/skill_returns/*.json
    #   pinned-by tests/cli/test_skill_returns.py::test_known_skills_match_schema_files

Deferred allowlist
------------------

Promoting all ~85 legacy mirror comments to structured annotations is a
burn-down, not a single change. ``scripts/mirror_ledger_allowlist.txt`` lists
the files whose mirror comments are *not yet* promoted; they are exempt for
now. Two guardrails keep the allowlist honest:

* A **new** mirror comment in a file that is NOT allowlisted fires immediately
  (the fire path) — so the debt cannot grow silently.
* A **stale** allowlist entry — a listed file that no longer contains any
  mirror comment (the comment was deleted or annotated) — also fires, forcing
  the entry to be removed. The ledger shrinks, never rots.

Exits 1 with one ``path:line: <hint>`` per finding; exits 0 when clean. The
fire path is exercised in ``tests/scripts/test_lint_mirror_ledger.py``.
"""

from __future__ import annotations

import ast
import io
import re
import sys
import tokenize
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Source tree that ships to users. Repo-relative so the test can point the
# scan at a synthetic tree.
_SCAN_ROOT_REL = Path("src/hpc_agent")
_ALLOWLIST_REL = Path("scripts/mirror_ledger_allowlist.txt")

# Trigger phrases that mark a comment as a mirror/duplication promise. Matched
# case-insensitively against comment + string tokens (so identifiers named
# ``mirrors`` in code never false-trigger — only prose does).
_TRIGGER = re.compile(r"kept in sync|lock-?step|\bmirrors\b", re.IGNORECASE)

# The structured annotation: ``# MIRROR: <twin> pinned-by <test>``. Both the
# twin and the test locator must be non-empty. ``pinned-by`` / ``pinned by``
# both accepted.
_ANNOTATION = re.compile(r"MIRROR:\s*(?P<twin>.+?)\s+pinned[ -]by\s+(?P<test>\S.*)", re.IGNORECASE)

# How many lines away from a mirror comment the MIRROR annotation may sit and
# still count as "accompanying" it. A mirror comment and its annotation live in
# the same comment block; this window tolerates a multi-line block between them.
_ANNOTATION_WINDOW = 10


def _docstring_line_ranges(source: str) -> list[tuple[int, int]]:
    """(start, end) 1-based line ranges of every docstring in *source*.

    A "mirror" promise is documentation — it lives in a ``#`` comment or a
    docstring, not in an arbitrary runtime message string. Restricting the
    string surface to docstrings (module / class / def) keeps user-facing
    ``_err(message=...)`` prose that happens to say "mirrors" out of scope.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    ranges: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ranges.append((first.lineno, getattr(first, "end_lineno", first.lineno)))
    return ranges


def _trigger_lines(path: Path) -> set[int]:
    """1-based line numbers carrying a mirror trigger phrase, in documentation only.

    The scanned surface is comments (``tokenize`` COMMENT tokens) plus docstring
    line ranges (via AST). Code identifiers and runtime message strings are out
    of scope — only the *documented* mirror convention is enforced.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set()
    source_lines = source.splitlines()
    lines: set[int] = set()

    # Comments.
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type != tokenize.COMMENT:
                continue
            # A MIRROR annotation is itself a comment containing "mirror"; do
            # not count the annotation line as a mirror comment to satisfy.
            if _ANNOTATION.search(tok.string):
                continue
            if _TRIGGER.search(tok.string):
                lines.add(tok.start[0])
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # Unparseable: raw fallback so a mirror comment is never silently
        # missed (over-reporting is safe — it forces a review or allowlisting).
        for lineno, text in enumerate(source_lines, start=1):
            if _TRIGGER.search(text) and not _ANNOTATION.search(text):
                lines.add(lineno)
        return lines

    # Docstrings.
    for start, end in _docstring_line_ranges(source):
        for lineno in range(start, min(end, len(source_lines)) + 1):
            text = source_lines[lineno - 1]
            if _TRIGGER.search(text) and not _ANNOTATION.search(text):
                lines.add(lineno)
    return lines


def _annotation_lines(path: Path) -> set[int]:
    """1-based line numbers carrying a well-formed MIRROR annotation."""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set()
    out: set[int] = set()
    for lineno, text in enumerate(source.splitlines(), start=1):
        m = _ANNOTATION.search(text)
        if m and m.group("twin").strip() and m.group("test").strip():
            out.add(lineno)
    return out


def _load_allowlist(repo: Path) -> set[str]:
    """Posix repo-relative paths deferred from the structured-annotation rule."""
    path = repo / _ALLOWLIST_REL
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set()
    entries: set[str] = set()
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        entries.add(stripped)
    return entries


def _iter_sources(repo: Path) -> list[Path]:
    root = repo / _SCAN_ROOT_REL
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*.py") if p.is_file())


def lint(repo: Path) -> list[str]:
    """Return one message per unsatisfied mirror comment or stale allowlist entry."""
    allowlist = _load_allowlist(repo)
    findings: list[str] = []
    seen_allowlisted: set[str] = set()

    for path in _iter_sources(repo):
        rel = path.relative_to(repo).as_posix()
        triggers = _trigger_lines(path)
        if rel in allowlist:
            # Deferred: exempt from the annotation rule, but the entry must
            # still be earning its place — a listed file with no mirror
            # comment left is stale and must be removed.
            if triggers:
                seen_allowlisted.add(rel)
            continue
        if not triggers:
            continue
        annotations = _annotation_lines(path)
        for line in sorted(triggers):
            near = any(abs(line - a) <= _ANNOTATION_WINDOW for a in annotations)
            if not near:
                findings.append(
                    f"{rel}:{line}: mirror comment ('kept in sync' / 'lock-step' / "
                    f"'mirrors X') without a '# MIRROR: <twin> pinned-by <test>' "
                    f"annotation nearby, and the file is not in "
                    f"{_ALLOWLIST_REL.as_posix()}. Name the twin + its pinning test, "
                    f"or defer it by listing the file in the allowlist."
                )

    # Stale allowlist entries: listed but no longer carrying any mirror comment.
    for rel in sorted(allowlist):
        source = repo / rel
        if rel in seen_allowlisted:
            continue
        if not source.exists():
            findings.append(
                f"{_ALLOWLIST_REL.as_posix()}: stale entry {rel!r} — file no longer "
                f"exists; remove it from the allowlist."
            )
        elif not _trigger_lines(source):
            findings.append(
                f"{_ALLOWLIST_REL.as_posix()}: stale entry {rel!r} — file has no "
                f"mirror comment anymore; remove it from the allowlist (the ledger "
                f"shrinks as comments are promoted or deleted)."
            )
    return findings


def main(repo: Path | None = None) -> int:
    root = repo if repo is not None else REPO
    findings = lint(root)
    for msg in findings:
        print(msg)
    if findings:
        print(
            f"\n{len(findings)} mirror-ledger issue(s). Every 'kept in sync' / "
            f"'lock-step' / 'mirrors X' comment must name its twin and the test that "
            f"fails on drift, via a '# MIRROR: <twin> pinned-by <test>' annotation — "
            f"turning the sync convention into a checked contract (N7). Legacy comments "
            f"are deferred via {_ALLOWLIST_REL.as_posix()}; a new one in a "
            f"non-allowlisted file must be annotated.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
