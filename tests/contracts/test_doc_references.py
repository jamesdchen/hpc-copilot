"""CI pin: doc-referenced console scripts and module paths must exist.

Motivation (the drift class this pin catches). The async-refill series
deleted the campaign driver, but ``docs/internals/campaign-lifecycle.md``
kept calling ``hpc-campaign-driver`` a "console script" long after it was
removed from ``pyproject``'s ``[project.scripts]``; separately, doc prose
routinely cites ``src/hpc_agent/...`` module paths that a refactor has
since moved (e.g. a bug-sweep record pointing at
``ops/doctor_install.py`` when the op actually lives at
``ops/recover/doctor_install.py``). Both are silent rot: nothing fails
until a reader follows the dead reference. This pin turns the two
regressions into a test.

Scope — ``docs/internals/`` + ``docs/workflows/`` ONLY (the operational
truth surfaces). ``docs/design/`` + ``docs/plans/`` narrate history by
design and are deliberately out of scope (architect memo §6). Within
scope, two exclusions keep the pin honest rather than noisy:

* **Fenced code blocks** (```` ``` ````-delimited) are masked. They carry
  worked examples and shell transcripts whose tokens are illustrative,
  not load-bearing operational claims.
* **Drift-log sections** (a heading whose text contains "drift log", up
  to the next same-or-shallower heading) are masked. Their whole purpose
  is to record paths/scripts that USED to exist — scanning them would
  fault the very honesty they provide.

Both exclusions preserve line numbers (masked regions become blank lines)
so failure messages still point at the right line.

Honest scope caveat. The console-script half is **precision-first**: the
``hpc-`` prefix is heavily overloaded in this repo (skills
``hpc-submit``/``hpc-status``, a docker tag ``hpc-agent-slurm-ci``, plugin
dirs ``hpc-agent-vastai``, the repo name ``hpc-copilot``), so a blanket
"every ``hpc-*`` token is a console script" rule would be almost all
false positives. This pin only classifies a token as a console-script
reference when it is either (a) **invoked** — the first token of an inline
code span, followed by a subcommand/flag — or (b) **labelled** — the
nearest ``hpc-`` token preceding the literal phrase "console script".
Those are exactly the two shapes the real drift took; a bare mention that
is neither is intentionally not classified (that surface is code-fenced
invocation, excluded above). Allowlisted exceptions carry a cited reason.

Stdlib only (``re``, ``pathlib``); scripts are read from ``pyproject``
via a tiny section parser so no toml dependency is assumed on 3.10.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests._paths import REPO_ROOT

# Operational-truth doc surfaces. Design/plans narrate history — out of scope.
SCOPE_DIRS: tuple[str, ...] = ("docs/internals", "docs/workflows")


# ---------------------------------------------------------------------------
# Allowlists — each entry cites WHY the reference is legitimately absent.
# ---------------------------------------------------------------------------

# Console-script names that appear labelled/invoked in scope but are NOT in
# ``[project.scripts]`` for a documented reason. Keyed by script name.
CONSOLE_SCRIPT_ALLOWLIST: dict[str, str] = {
    # The campaign driver was deleted in the worker-removal wave; the
    # campaign docs (owned by the async-refill series — campaign-lifecycle.md
    # carries a historical banner, campaign.md is the campaign README) retain
    # the name as documented history of a shape that no longer ships. Reported
    # to the integrator; drop this entry if those docs stop calling it a
    # console script.
    "hpc-campaign-driver": (
        "deleted in the worker-removal wave; retained as documented history "
        "in the async-refill-owned campaign docs"
    ),
}

# ``src/hpc_agent/...`` path references that are legitimately absent on disk.
# Keyed by (doc relative to repo root, normalised ``src/hpc_agent/...`` ref).
# Empty: the d-rewrite entry for submit-sequence.md's ``worker_prompts/submit.md``
# ref was dropped once d-rewrite landed — the rewrite now names the deleted
# prompt only in the bare ``worker_prompts/submit.md`` form, which the
# ``hpc_agent/``-anchored detector does not yield, so no allowlisting is needed.
MODULE_PATH_ALLOWLIST: dict[tuple[str, str], str] = {
    # bug-sweep-2026-07-11 is a HISTORICAL record: it cites
    # relay_audit_stop.py:571 / :300 as they were on that date. The module was
    # later split into the relay_audit_stop/ package, so the exact-line
    # references no longer resolve — but rewriting a dated bug record with new
    # package paths + guessed line numbers would falsify the archive. Allowlist
    # the stale historical path rather than revise history.
    (
        "docs/internals/bug-sweep-2026-07-11.md",
        "src/hpc_agent/_kernel/hooks/relay_audit_stop.py",
    ): "historical: file split into the relay_audit_stop/ package after this sweep",
}


# ---------------------------------------------------------------------------
# Masking helpers (preserve line numbers so failures cite the right line).
# ---------------------------------------------------------------------------

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
    out: list[Path] = []
    for rel in SCOPE_DIRS:
        d = REPO_ROOT / rel
        if d.is_dir():
            out.extend(p for p in sorted(d.rglob("*.md")) if p.is_file())
    return out


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


# ---------------------------------------------------------------------------
# pyproject [project.scripts] — the source of truth for console-script names.
# ---------------------------------------------------------------------------

_SCRIPTS_KEY_RE = re.compile(r"^\s*([A-Za-z0-9._-]+)\s*=")


def _console_scripts() -> frozenset[str]:
    """Parse the ``[project.scripts]`` table keys from ``pyproject.toml``.

    A three-line hand parser (read keys between ``[project.scripts]`` and the
    next table header) so the pin needs no toml library on 3.10.
    """
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    names: set[str] = set()
    in_section = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            in_section = line == "[project.scripts]"
            continue
        if in_section:
            m = _SCRIPTS_KEY_RE.match(raw)
            if m:
                names.add(m.group(1))
    return frozenset(names)


CONSOLE_SCRIPTS = _console_scripts()


# ---------------------------------------------------------------------------
# Reference extractors.
# ---------------------------------------------------------------------------

_INLINE_SPAN_RE = re.compile(r"`([^`\n]+)`")
_INVOCATION_RE = re.compile(r"^(hpc-[a-z0-9-]+)\s+\S")
_LABEL_RE = re.compile(r"console[- ]script", re.IGNORECASE)
_HPC_TOKEN_RE = re.compile(r"hpc-[a-z0-9-]+")
_LABEL_WINDOW = 48  # chars scanned back from "console script" for the name


def _console_script_references(text: str) -> list[tuple[int, str]]:
    """Return ``(line, script_name)`` for every console-script reference.

    Two shapes (see the module docstring's honest-scope caveat):
    * invocation — first token of an inline code span, followed by more;
    * label — the nearest ``hpc-`` token before the phrase "console script".
    """
    refs: list[tuple[int, str]] = []
    for m in _INLINE_SPAN_RE.finditer(text):
        inv = _INVOCATION_RE.match(m.group(1).strip())
        if inv:
            refs.append((_line_of(text, m.start()), inv.group(1)))
    for m in _LABEL_RE.finditer(text):
        pre = text[max(0, m.start() - _LABEL_WINDOW) : m.start()]
        toks = _HPC_TOKEN_RE.findall(pre)
        if toks:
            refs.append((_line_of(text, m.start()), toks[-1]))
    return refs


# ``src/hpc_agent/...`` path references, matched with or without the ``src/``
# prefix and even inside relative links (``../../src/hpc_agent/...``); anchored
# on ``hpc_agent/`` so the on-disk check is ``<repo>/src/hpc_agent/<rest>``.
_MODULE_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])(?:src/)?(hpc_agent/[A-Za-z0-9_./-]+)")
_TRAILING = ".`)/,;:'\""


def _module_path_references(text: str) -> list[tuple[int, str]]:
    """Return ``(line, normalised_src_ref)`` for each ``hpc_agent/...`` path."""
    refs: list[tuple[int, str]] = []
    for m in _MODULE_PATH_RE.finditer(text):
        rest = m.group(1).rstrip(_TRAILING)
        refs.append((_line_of(text, m.start()), "src/" + rest))
    return refs


# ---------------------------------------------------------------------------
# Tree pins.
# ---------------------------------------------------------------------------


def test_console_scripts_parsed_from_pyproject() -> None:
    """Guard the hand parser: the known entry point must be found. A
    silently-empty set would make the console-script pin vacuous."""
    assert "hpc-agent" in CONSOLE_SCRIPTS, (
        "parsed [project.scripts] is missing hpc-agent; the section parser "
        f"in this test drifted from pyproject. Parsed: {sorted(CONSOLE_SCRIPTS)}"
    )


def test_console_script_references_exist() -> None:
    """Every console-script name referenced in scope exists in
    ``[project.scripts]`` (or is allowlisted with a cited reason)."""
    violations: list[str] = []
    for doc in _scope_docs():
        rel = doc.relative_to(REPO_ROOT).as_posix()
        for line, name in _console_script_references(_mask(doc.read_text(encoding="utf-8"))):
            if name in CONSOLE_SCRIPTS or name in CONSOLE_SCRIPT_ALLOWLIST:
                continue
            violations.append(f"  {rel}:{line}: `{name}` not in [project.scripts]")
    assert not violations, (
        "docs reference console scripts absent from pyproject [project.scripts]:\n"
        + "\n".join(violations)
        + "\n\nFix the doc to name a real script, add the script to "
        "[project.scripts], or (if it is documented history) add it to "
        "CONSOLE_SCRIPT_ALLOWLIST with a cited reason."
    )


def test_module_path_references_exist() -> None:
    """Every ``src/hpc_agent/...`` path referenced in scope exists on disk
    (or is allowlisted with a cited reason)."""
    violations: list[str] = []
    for doc in _scope_docs():
        rel = doc.relative_to(REPO_ROOT).as_posix()
        for line, ref in _module_path_references(_mask(doc.read_text(encoding="utf-8"))):
            if (REPO_ROOT / ref).exists():
                continue
            if (rel, ref) in MODULE_PATH_ALLOWLIST:
                continue
            violations.append(f"  {rel}:{line}: {ref} does not exist")
    assert not violations, (
        "docs reference src/hpc_agent paths that do not exist on disk:\n"
        + "\n".join(violations)
        + "\n\nFix the doc to name the real path, or (if the path is "
        "legitimately gone) add (doc, ref) to MODULE_PATH_ALLOWLIST with a "
        "cited reason."
    )


def test_allowlisted_module_paths_are_really_absent() -> None:
    """A stale allowlist is drift too: if an allowlisted path now EXISTS,
    the entry lies and should be removed. (Absent-but-unreferenced entries
    are tolerated — merge order across units decides when a reference goes.)"""
    stale = [f"  {ref}" for (_doc, ref) in MODULE_PATH_ALLOWLIST if (REPO_ROOT / ref).exists()]
    assert not stale, (
        "MODULE_PATH_ALLOWLIST entries whose path now exists — remove them:\n" + "\n".join(stale)
    )


# ---------------------------------------------------------------------------
# Fire-path tests — the guards must demonstrably fire on synthetic drift.
# ---------------------------------------------------------------------------


def test_console_script_check_fires_on_synthetic_violation(tmp_path: Path) -> None:
    """A doc invoking / labelling a non-existent console script is caught."""
    doc = tmp_path / "synthetic.md"
    doc.write_text(
        "# synthetic\n\n"
        "Run `hpc-bogus-verb --now` to start it.\n"
        "The `hpc-phantom-driver` console script drives the loop.\n",
        encoding="utf-8",
    )
    refs = _console_script_references(_mask(doc.read_text(encoding="utf-8")))
    names = {name for _line, name in refs}
    assert "hpc-bogus-verb" in names, "invocation-shaped reference not detected"
    assert "hpc-phantom-driver" in names, "console-script-labelled reference not detected"
    bad = [n for n in names if n not in CONSOLE_SCRIPTS and n not in CONSOLE_SCRIPT_ALLOWLIST]
    assert set(bad) == {"hpc-bogus-verb", "hpc-phantom-driver"}


def test_console_script_check_ignores_skills_and_masked_regions(tmp_path: Path) -> None:
    """Skill names (not invoked, not labelled) and fenced blocks do not
    false-positive — the precision claim in the docstring must hold."""
    doc = tmp_path / "quiet.md"
    doc.write_text(
        "# quiet\n\n"
        "The `hpc-submit` skill composes the submit flow.\n"  # bare skill mention
        "See `hpc-wrap-entry-point` for decoration.\n"
        "```bash\nhpc-ghost-command run everything\n```\n",  # fenced → masked
        encoding="utf-8",
    )
    refs = _console_script_references(_mask(doc.read_text(encoding="utf-8")))
    assert refs == [], f"expected no console-script references, got {refs}"


def test_module_path_check_fires_on_synthetic_violation(tmp_path: Path) -> None:
    """A doc citing a non-existent src/hpc_agent path is caught, and a
    real path plus a drift-log mention are not."""
    doc = tmp_path / "paths.md"
    doc.write_text(
        "# paths\n\n"
        "Broken: `src/hpc_agent/ops/does_not_exist.py`.\n"
        "Real: `src/hpc_agent/cli/dispatch.py`.\n\n"
        "## Drift log\n\n"
        "Historic: `src/hpc_agent/ops/vanished_module.py` (removed).\n",
        encoding="utf-8",
    )
    masked = _mask(doc.read_text(encoding="utf-8"))
    missing = [
        ref for _line, ref in _module_path_references(masked) if not (REPO_ROOT / ref).exists()
    ]
    assert "src/hpc_agent/ops/does_not_exist.py" in missing
    assert "src/hpc_agent/cli/dispatch.py" not in missing  # real path resolves
    assert "src/hpc_agent/ops/vanished_module.py" not in [
        ref for _l, ref in _module_path_references(masked)
    ], "drift-log section was not masked"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
