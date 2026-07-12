"""CI pin: ``docs/design/*.md`` status headers stay honest.

Motivation (the drift class this pin catches). A design doc records its
lifecycle in two places that must not disagree: a machine-readable
``status:`` in the YAML frontmatter, and a human-readable ``**Status: …**``
banner in the opening prose. The async-refill series exposed the failure
mode — a doc whose frontmatter said one thing while the body claimed a
feature had landed. Two regressions follow from that:

1. **Vocabulary drift** — the frontmatter ``status:`` value wanders into
   ad-hoc synonyms (``implemented``, ``planned``, ``design``) so no
   consumer (a docs index, a release checklist, this suite) can filter on
   it. This pin closes the value to the ratified set (architect memo §6):

       {plan, shipped, superseded, partial}

2. **Banner drift** — a doc still marked ``status: plan`` opens with an
   IMPLEMENTED/landed banner, silently overclaiming. This pin refuses a
   plan doc whose opening ``**Status: …**`` banner asserts done-ness.

Both checks are **regex-level**, with two deliberate scope caveats stated
honestly here rather than papered over:

* Only docs that DECLARE a frontmatter ``status:`` are vocabulary-checked.
  A doc with no frontmatter (some banked design notes carry only a body
  ``Status: **BANKED**`` line) is out of scope — this pin does not mandate
  that every design doc grow frontmatter.
* The banner check reads only the OPENING region (frontmatter → first
  ``## `` heading) and keys on the ``**Status: WORD**`` convention. A plan
  doc that mentions, mid-body, that some *other* feature landed is not
  faulted; a plan doc with no banner at all trivially passes.

Allowlisted frontmatter values carry a cited reason (a research-facts
reference doc that is not a lifecycle record; a doc owned by another unit).

Stdlib only (``re``, ``pathlib``).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests._paths import REPO_ROOT

DESIGN_DIR = REPO_ROOT / "docs" / "design"

# The ratified closed vocabulary (architect memo §6).
STATUS_VOCABULARY: frozenset[str] = frozenset({"plan", "shipped", "superseded", "partial"})

# Banner words that assert a feature is DONE. A ``status: plan`` doc must
# not open with any of these.
DONE_BANNER_WORDS: frozenset[str] = frozenset({"implemented", "landed", "shipped", "built"})


# ---------------------------------------------------------------------------
# Allowlist — (doc relative to repo root) -> cited reason. These docs carry a
# frontmatter status outside the closed vocabulary for a documented reason.
# ---------------------------------------------------------------------------

STATUS_VALUE_ALLOWLIST: dict[str, str] = {
    # A research-facts reference (MCP spec extract), not a feature-lifecycle
    # doc — {plan, shipped, superseded, partial} cannot express it. Left as
    # `status: reference` deliberately.
    "docs/design/mcp-elicitation-facts.md": (
        "research-facts reference doc, not a feature lifecycle record"
    ),
    # Owned by the async-refill series (BACKGROUND FACT). Its
    # `partially-implemented` value is semantically `partial` but the file is
    # out of this unit's ownership. Reported to the integrator.
    "docs/design/campaign-async-refill.md": (
        "owned by the async-refill series; value should normalise to `partial`"
    ),
    # Frontmatter says `implemented (dark — capability-gated; see drift log)`
    # while the body banner says DESIGN with the build pending — a genuine
    # frontmatter/body contradiction, and no single closed-vocabulary word
    # expresses "implemented but dark/gated". Left as-is and reported for
    # owner reconciliation.
    "docs/design/stop-hook-completer.md": (
        "capability-gated/dark qualifier + body/frontmatter contradiction; "
        "no closed-vocabulary word expresses it — reported for reconciliation"
    ),
}


# ---------------------------------------------------------------------------
# Parsing helpers.
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_STATUS_RE = re.compile(r"^status:\s*(.+?)\s*$", re.MULTILINE)
_BANNER_RE = re.compile(r"\*\*\s*Status\s*:?\s*\*{0,2}\s*([A-Za-z][A-Za-z-]*)", re.IGNORECASE)


def _frontmatter_status(text: str) -> str | None:
    """Return the raw frontmatter ``status:`` value, or None if absent."""
    fm = _FRONTMATTER_RE.match(text)
    if not fm:
        return None
    sm = _STATUS_RE.search(fm.group(1))
    return sm.group(1).strip() if sm else None


def _opening_after_frontmatter(text: str) -> str:
    """Return the prose from just after the frontmatter to the first ``## ``
    heading (the region where an opening status banner lives)."""
    fm = _FRONTMATTER_RE.match(text)
    body = text[fm.end() :] if fm else text
    cut = body.find("\n## ")
    return body if cut < 0 else body[:cut]


def _opening_banner_word(text: str) -> str | None:
    """Return the first ``**Status: WORD**`` banner word in the opening, or
    None if the doc opens with no such banner."""
    m = _BANNER_RE.search(_opening_after_frontmatter(text))
    return m.group(1).lower() if m else None


def _design_docs() -> list[Path]:
    if not DESIGN_DIR.is_dir():
        return []
    # history/ narrates history by design — never in scope.
    return [
        p for p in sorted(DESIGN_DIR.glob("*.md")) if p.is_file() and p.parent.name != "history"
    ]


# ---------------------------------------------------------------------------
# Tree pins.
# ---------------------------------------------------------------------------


def test_design_docs_exist() -> None:
    """Sanity: the scan target is non-empty (a vacuous pin is worthless)."""
    assert _design_docs(), f"no design docs found under {DESIGN_DIR}"


def test_frontmatter_status_in_vocabulary() -> None:
    """Every ``docs/design/*.md`` that declares a frontmatter ``status:``
    uses a value from the closed vocabulary (or is allowlisted)."""
    violations: list[str] = []
    for doc in _design_docs():
        rel = doc.relative_to(REPO_ROOT).as_posix()
        value = _frontmatter_status(doc.read_text(encoding="utf-8"))
        if value is None:
            continue  # no frontmatter status — out of scope (see docstring)
        if value in STATUS_VOCABULARY or rel in STATUS_VALUE_ALLOWLIST:
            continue
        violations.append(f"  {rel}: status: {value!r}")
    assert not violations, (
        "design-doc frontmatter status values outside the closed vocabulary "
        f"{sorted(STATUS_VOCABULARY)}:\n"
        + "\n".join(violations)
        + "\n\nNormalise to a vocabulary value, or (if the doc is genuinely "
        "not a feature-lifecycle record) add it to STATUS_VALUE_ALLOWLIST "
        "with a cited reason."
    )


def test_plan_docs_do_not_open_with_a_done_banner() -> None:
    """A ``status: plan`` doc must not open with an IMPLEMENTED/landed
    banner — that is the overclaim the async-refill drift exposed."""
    violations: list[str] = []
    for doc in _design_docs():
        rel = doc.relative_to(REPO_ROOT).as_posix()
        text = doc.read_text(encoding="utf-8")
        if _frontmatter_status(text) != "plan":
            continue
        banner = _opening_banner_word(text)
        if banner in DONE_BANNER_WORDS:
            violations.append(f"  {rel}: status: plan but opens with **Status: {banner.upper()}**")
    assert not violations, (
        "status: plan docs opening with a done banner (frontmatter and body "
        "disagree):\n"
        + "\n".join(violations)
        + "\n\nEither the doc has shipped (fix frontmatter to `shipped`) or "
        "the banner overclaims (fix the banner)."
    )


def test_status_value_allowlist_entries_resolve() -> None:
    """Every allowlisted doc must exist and still carry a non-vocabulary
    status — a stale allowlist entry (doc gone, or value now normalised) is
    itself drift and should be removed."""
    stale: list[str] = []
    for rel in STATUS_VALUE_ALLOWLIST:
        p = REPO_ROOT / rel
        if not p.is_file():
            stale.append(f"  {rel}: file no longer exists")
            continue
        value = _frontmatter_status(p.read_text(encoding="utf-8"))
        if value in STATUS_VOCABULARY:
            stale.append(f"  {rel}: value {value!r} is now in-vocabulary; drop the entry")
    assert not stale, "STATUS_VALUE_ALLOWLIST has stale entries:\n" + "\n".join(stale)


# ---------------------------------------------------------------------------
# Fire-path tests — the guards must demonstrably fire on synthetic drift.
# ---------------------------------------------------------------------------


def test_vocabulary_check_fires_and_respects_scope(tmp_path: Path) -> None:
    """A bad frontmatter value is caught; a vocabulary value and a
    no-frontmatter doc are not."""
    bad = tmp_path / "bad.md"
    bad.write_text("---\nstatus: kinda-done\n---\n# bad\n", encoding="utf-8")
    good = tmp_path / "good.md"
    good.write_text("---\nstatus: shipped\n---\n# good\n", encoding="utf-8")
    none = tmp_path / "none.md"
    none.write_text("# none\n\nStatus: **BANKED** (a body-only note).\n", encoding="utf-8")

    assert _frontmatter_status(bad.read_text()) == "kinda-done"
    assert _frontmatter_status(bad.read_text()) not in STATUS_VOCABULARY
    assert _frontmatter_status(good.read_text()) in STATUS_VOCABULARY
    assert _frontmatter_status(none.read_text()) is None  # out of scope, not a violation


def test_banner_check_fires_and_ignores_negations(tmp_path: Path) -> None:
    """A plan doc opening with an IMPLEMENTED banner is caught; a plan doc
    whose banner says PLANNED (even 'not yet implemented') is not."""
    overclaim = tmp_path / "overclaim.md"
    overclaim.write_text(
        "---\nstatus: plan\n---\n# x\n\n**Status: IMPLEMENTED (landed 2026).** body\n",
        encoding="utf-8",
    )
    honest = tmp_path / "honest.md"
    honest.write_text(
        "---\nstatus: plan\n---\n# y\n\n**Status: PLANNED, not yet implemented.** body\n",
        encoding="utf-8",
    )
    assert _opening_banner_word(overclaim.read_text()) == "implemented"
    assert _opening_banner_word(overclaim.read_text()) in DONE_BANNER_WORDS
    assert _opening_banner_word(honest.read_text()) == "planned"
    assert _opening_banner_word(honest.read_text()) not in DONE_BANNER_WORDS


def test_banner_check_ignores_prose_landed_outside_banner(tmp_path: Path) -> None:
    """A plan doc that mentions another feature 'landed' in prose (not in a
    **Status: …** banner) must not false-positive."""
    doc = tmp_path / "prose.md"
    doc.write_text(
        "---\nstatus: plan\n---\n# z\n\n"
        "**Status: DRAFT.** Depends on the conformance kit, which has LANDED.\n",
        encoding="utf-8",
    )
    assert _opening_banner_word(doc.read_text()) == "draft"
    assert _opening_banner_word(doc.read_text()) not in DONE_BANNER_WORDS


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
