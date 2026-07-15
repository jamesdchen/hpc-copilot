"""Contract (plan A6): frozen count literals in operational docs equal live.

The drift class. ``docs/internals/adding-a-primitive.md`` opened with "the
existing 167 primitives" while the registry had already grown to 169 — a
frozen literal that rots silently every time the counted set changes. The
older :mod:`tests.contracts.test_prose_primitive_count` pin only watched
``N primitives`` and only to within ±2, so a two-off literal (exactly this
case) slipped through. This pin freezes the whole family — primitives,
verbs, schemas, error codes, regen scripts — to STRICT equality against the
live count, in the operational-truth doc surfaces (``docs/internals`` +
``docs/workflows``; design/plans narrate history and are out of scope).

Live count sources (each derived, never hardcoded):

* **primitives** = ``len(core_only_registry())`` — the exact set
  :mod:`test_prose_primitive_count` compares, core-only so a dev-shell
  plugin can't skew it.
* **verbs** = ``len(core_only_operations_catalog())`` — the registry count.
  This matches the repo's own prose convention: the 2026-07-13 architecture
  review corrected a doc's "167 verbs" to **169** (the registry size), NOT
  to the CLI-exposed subset. The CLI-exposed subset (``cli``-truthy
  entries, 159 today) is a DIFFERENT quantity; a doc that means it must say
  so and allowlist. Integrator ruling folded in: "verbs" == registry size.
* **schemas** = RECURSIVE glob of ``src/hpc_agent/schemas/**/*.json`` (249
  today — INCLUDING the five ``skill_returns/*.json`` sub-schemas). The
  recursive count is deliberate: the sub-schemas are shipped schemas too.
* **error codes** = the ``$defs.ErrorEnvelope.properties.error_code.enum``
  length parsed from ``schemas/envelope.json`` (stdlib json); the byte
  alignment of that enum with the other SoTs is held by
  ``test_error_code_sots_aligned``.
* **regen scripts** = the length of the module-level ``REGEN_SCRIPTS`` tuple
  in ``scripts/regen_all.py`` (the frozen seam WS1/plan-A1 exports; read by
  static ``ast`` parse, no import side effects). That file is built by WS1
  and merges BEFORE this unit, so at merge time it is present; on this
  unit's pre-merge branch it is absent and a LOUD, CITED fallback counts the
  ``name: regenerate …`` pre-commit hooks instead (6 today). See
  :func:`_live_regen_script_count`.

Scope + masking are the shared seam in :mod:`tests.contracts._doc_scan`
(fenced blocks and drift-log sections are masked — they legitimately carry
historical counts). Fenced/drift-log masking plus STRICT equality with a
cited allowlist is the same d-pins discipline
:mod:`test_doc_references` uses.

Line-based, not full-text. The scan runs the count regex PER LINE, never
across the whole document: a full-text ``finditer`` lets the ``\\s+`` between
the digit and the noun span a newline, so a digit ending one line and a noun
opening the next would false-positive. The one real violation this pin was
built for (``adding-a-primitive.md`` "167 primitives") has the digit and
noun on the SAME line, so line-based scanning is both sufficient and
false-positive-free; a count literal that authors deliberately wrap across a
line break is out of scope (documented limitation, exercised below).

Approximate claims. The regex captures an optional ``~`` prefix; under
strict equality ``~170 primitives`` is treated as a claim of exactly 170 and
fails when live is 169. Write the live number, or allowlist. (This is
stricter than the old ±2 pin, on purpose.)

Relationship to :mod:`test_prose_primitive_count`. For the in-scope dirs
this pin strictly dominates that one (exact vs ±2, and the whole count
family vs primitives only), so that pin was narrowed to stop scanning
``docs/internals`` — it now covers only ``README.md`` + ``docs/reference``,
which A6's scope excludes.

Out-of-scope near-miss (documented, deliberately unclassified): the WORD
form "The six regen scripts" (``docs/internals/regen-debt-ledger.md``) is
not a digit literal, so the digit-only regex does not — and should not —
match it; word-number matching drags in structural prose ("the two-primitive
loop") that is not a registry claim.
"""

from __future__ import annotations

import ast
import json
import re
import warnings
from pathlib import Path

import pytest

from tests._paths import REPO_ROOT, SCHEMAS_DIR
from tests._registry_helpers import core_only_operations_catalog, core_only_registry
from tests.contracts._doc_scan import _mask, _scope_docs

# ---------------------------------------------------------------------------
# Allowlist — each entry cites WHY the frozen count is legitimate.
# ---------------------------------------------------------------------------

# Keyed by (doc relative to repo root, exact matched substring) -> cited
# rationale. Default policy is fail-loud STRICT equality; an entry here is
# for prose where the number is a deliberate fixed/historical reference.
#
# Currently empty. The ONE live digit violation in scope —
# ``docs/internals/adding-a-primitive.md`` "167 primitives" — is NOT
# allowlisted here: that file is owned by WS1 (plan A7), which rewrites the
# literal to verify-live phrasing and merges BEFORE this unit, so on the
# merged tree there is no violation to allowlist. On this unit's own
# pre-merge branch the real-tree pin below is RED on exactly that one line
# (disclosed to the integrator); it goes green the moment WS1 is in the tree.
_COUNT_ALLOWLIST: dict[tuple[str, str], str] = {}


# ---------------------------------------------------------------------------
# Live count sources (derived — never a hardcoded literal).
# ---------------------------------------------------------------------------

# The noun family. Each maps (after singular/plural + space normalisation) to
# a live-count key. ``error codes`` / ``regen scripts`` carry an internal
# space — line-based scanning keeps that space on one line, so it is safe.
_NOUN_TO_KEY: dict[str, str] = {
    "primitive": "primitives",
    "verb": "verbs",
    "schema": "schemas",
    "error code": "error_codes",
    "regen script": "regen",
}

# ``~?`` optional approximate prefix; digit group; one-or-more spaces; noun.
# Singular/plural via optional trailing ``s``. Line-based (fed one line at a
# time) so ``\s+`` can never cross a newline.
_COUNT_RE = re.compile(r"~?\b(\d+)\s+(primitives?|verbs?|schemas?|error codes?|regen scripts?)\b")


def _normalise_noun(noun: str) -> str:
    """Map a matched noun ('primitives', 'error code', …) to its family key."""
    singular = noun[:-1] if noun.endswith("s") else noun
    # 'error codes' -> 'error code'; 'regen scripts' -> 'regen script'.
    return _NOUN_TO_KEY[singular]


def _parse_regen_scripts_tuple(path: Path) -> int:
    """Length of the module-level ``REGEN_SCRIPTS`` tuple/list, via ``ast``.

    Static parse (no import) so reading the seam has no regen side effects.
    The frozen seam (architect memo §3) guarantees a module-level
    ``REGEN_SCRIPTS`` literal collection.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets: list[ast.expr] = node.targets
            value: ast.expr | None = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == "REGEN_SCRIPTS" for t in targets):
            continue
        if isinstance(value, (ast.Tuple, ast.List)):
            return len(value.elts)
        raise AssertionError(
            f"{path}: REGEN_SCRIPTS is not a literal tuple/list "
            f"(got {type(value).__name__}); the frozen seam (memo §3) "
            "requires a module-level literal collection."
        )
    raise AssertionError(
        f"{path}: no module-level REGEN_SCRIPTS assignment found — the frozen "
        "seam (architect memo §3) requires WS1/plan-A1 to export it."
    )


def _precommit_regen_hook_count() -> int:
    """Count ``.pre-commit-config.yaml`` hooks named ``regenerate …``.

    The pre-A1 fallback source for the regen-script count (line-based, no
    YAML dependency): every regen step is a hook whose ``name:`` begins with
    ``regenerate``.
    """
    text = (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    return sum(1 for line in text.splitlines() if re.match(r"\s*name:\s*regenerate\b", line))


def _live_regen_script_count() -> int:
    """Live regen-script count from the frozen ``REGEN_SCRIPTS`` seam.

    Reads ``scripts/regen_all.py``'s ``REGEN_SCRIPTS`` tuple when present
    (the merged-tree path — WS1/plan-A1 merges before this unit). If the file
    is absent (this unit's pre-merge branch), fall back LOUDLY to counting the
    pre-commit ``regenerate`` hooks, and cite the coupling so the source of
    the number is never silent.
    """
    regen = REPO_ROOT / "scripts" / "regen_all.py"
    if regen.is_file():
        return _parse_regen_scripts_tuple(regen)
    warnings.warn(
        "scripts/regen_all.py absent — falling back to the pre-commit "
        "'regenerate' hook count for the live regen-script number. This is "
        "expected ONLY on WS4's pre-merge branch; WS1 (plan A1) builds "
        "regen_all.py with a module-level REGEN_SCRIPTS tuple and merges "
        "before WS4, so on the merged tree this fallback never runs.",
        stacklevel=2,
    )
    return _precommit_regen_hook_count()


def _live_counts() -> dict[str, int]:
    """The five live counts, each derived from its source of truth."""
    envelope = json.loads((SCHEMAS_DIR / "envelope.json").read_text(encoding="utf-8"))
    error_enum = envelope["$defs"]["ErrorEnvelope"]["properties"]["error_code"]["enum"]
    return {
        "primitives": len(core_only_registry()),
        "verbs": len(core_only_operations_catalog()),
        "schemas": len(list(SCHEMAS_DIR.rglob("*.json"))),
        "error_codes": len(error_enum),
        "regen": _live_regen_script_count(),
    }


# ---------------------------------------------------------------------------
# Line-based extractor.
# ---------------------------------------------------------------------------


def _count_claims(text: str) -> list[tuple[int, str, str, int]]:
    """Return ``(line, substring, family_key, claimed_int)`` per count claim.

    LINE-BASED: the regex runs against each line independently, so ``\\s+``
    can never span a newline (a wrapped ``167\\nprimitives`` is not a claim).
    """
    claims: list[tuple[int, str, str, int]] = []
    for i, line in enumerate(text.splitlines(), start=1):
        for m in _COUNT_RE.finditer(line):
            key = _normalise_noun(m.group(2))
            claims.append((i, m.group(0), key, int(m.group(1))))
    return claims


def _scan_violations(counts: dict[str, int]) -> list[str]:
    """Every non-allowlisted count claim in scope that != its live count."""
    violations: list[str] = []
    for doc in _scope_docs():
        rel = doc.relative_to(REPO_ROOT).as_posix()
        masked = _mask(doc.read_text(encoding="utf-8"))
        for line, substring, key, claimed in _count_claims(masked):
            if (rel, substring) in _COUNT_ALLOWLIST:
                continue
            live = counts[key]
            if claimed != live:
                violations.append(f"  {rel}:{line}: {substring!r} but live {key} = {live}")
    return violations


# ---------------------------------------------------------------------------
# Vacuity guards — a broken count source must not make the pin toothless.
# ---------------------------------------------------------------------------


def test_live_counts_are_sane() -> None:
    """Sane floors so a silently-broken source can't make the pin vacuous.

    Precedent: ``test_console_scripts_parsed_from_pyproject`` guards its own
    parser the same way.
    """
    counts = _live_counts()
    assert counts["primitives"] > 100, counts
    assert counts["verbs"] > 100, counts
    assert counts["schemas"] > 100, counts
    assert counts["error_codes"] > 0, counts
    assert counts["regen"] >= 5, counts


# ---------------------------------------------------------------------------
# Real-tree pin.
# ---------------------------------------------------------------------------


def test_frozen_counts_track_live() -> None:
    """Every ``N primitives|verbs|schemas|error codes|regen scripts`` literal
    in ``docs/internals`` + ``docs/workflows`` equals the live count (or is
    allowlisted with a cited reason).

    NOTE for the integrator: on WS4's pre-merge branch this is RED on exactly
    ``docs/internals/adding-a-primitive.md`` "167 primitives" (live 169). That
    file is WS1's (plan A7); WS1 rewrites the literal to verify-live phrasing
    and merges BEFORE WS4, so on the merged tree this is green with no
    allowlist entry.
    """
    violations = _scan_violations(_live_counts())
    assert not violations, (
        "docs freeze counts the live registry/schemas/regen set has "
        "outgrown:\n" + "\n".join(violations) + "\n\nFix the doc to the live "
        "number (or use verify-live phrasing), or add (doc, substring) to "
        "_COUNT_ALLOWLIST with a cited reason."
    )


def test_count_allowlist_not_stale() -> None:
    """A stale allowlist is drift too: an entry whose substring is still
    present in its doc AND whose digit now equals the live count is lying and
    should be removed. (Absent substrings are tolerated — merge order across
    units decides when a reference goes.) Mirror of
    ``test_allowlisted_module_paths_are_really_absent``."""
    counts = _live_counts()
    stale: list[str] = []
    for (rel, substring), _reason in _COUNT_ALLOWLIST.items():
        doc = REPO_ROOT / rel
        if not doc.is_file():
            continue
        masked = _mask(doc.read_text(encoding="utf-8"))
        present = any(sub == substring for _l, sub, _k, _c in _count_claims(masked))
        if not present:
            continue
        m = _COUNT_RE.search(substring)
        assert m is not None, substring
        if int(m.group(1)) == counts[_normalise_noun(m.group(2))]:
            stale.append(f"  ({rel!r}, {substring!r}) now equals the live count")
    assert not stale, (
        "_COUNT_ALLOWLIST entries whose count now matches live — remove them:\n" + "\n".join(stale)
    )


# ---------------------------------------------------------------------------
# Fire-path tests — the guard must demonstrably fire on synthetic drift.
# ---------------------------------------------------------------------------


def test_frozen_count_check_fires_on_synthetic_violation(tmp_path: Path) -> None:
    """Wrong digit literals (incl. an approximate one and the two-word nouns)
    are all detected against live counts."""
    counts = _live_counts()
    doc = tmp_path / "drift.md"
    doc.write_text(
        "# drift\n\n"
        "There are 42 primitives.\n"
        "Six became 3 regen scripts.\n"
        "About ~7 error codes exist.\n",
        encoding="utf-8",
    )
    masked = _mask(doc.read_text(encoding="utf-8"))
    claims = _count_claims(masked)
    keyed = {key: claimed for _l, _s, key, claimed in claims}
    assert keyed == {"primitives": 42, "regen": 3, "error_codes": 7}, claims
    # All three mismatch live (live primitives/regen/error_codes are none of
    # 42/3/7 in this repo), so all three are violations.
    mismatches = [(k, c) for _l, _s, k, c in claims if c != counts[k]]
    assert len(mismatches) == 3, (mismatches, counts)


def test_line_based_scan_ignores_wrapped_claim(tmp_path: Path) -> None:
    """Documented limitation: a count literal wrapped across a line break is
    line-based-invisible (the ``\\s+`` never crosses a newline). The one real
    violation this pin targets is single-line, so this is sufficient."""
    doc = tmp_path / "wrapped.md"
    doc.write_text("# wrapped\n\nThere are 42\nprimitives here.\n", encoding="utf-8")
    masked = _mask(doc.read_text(encoding="utf-8"))
    assert _count_claims(masked) == [], "wrapped claim must not match line-based"


def test_frozen_count_check_passes_on_exact_and_masked(tmp_path: Path) -> None:
    """Exact live literals pass; stale counts inside a fenced block and under a
    drift-log heading are masked (they legitimately carry historical numbers)."""
    counts = _live_counts()
    doc = tmp_path / "clean.md"
    doc.write_text(
        "# clean\n\n"
        f"The core surface has {counts['primitives']} primitives and "
        f"{counts['verbs']} verbs.\n"
        f"There are {counts['schemas']} schemas and {counts['error_codes']} "
        f"error codes, built by {counts['regen']} regen scripts.\n\n"
        "```text\n"
        "Historically there were 50 primitives.\n"  # fenced -> masked
        "```\n\n"
        "## Drift log\n\n"
        "Once we said 12 verbs.\n",  # drift-log -> masked
        encoding="utf-8",
    )
    masked = _mask(doc.read_text(encoding="utf-8"))
    rel_claims = _count_claims(masked)
    # The two masked stale numbers must be gone; the five exact ones remain
    # and all equal live.
    assert all(c == counts[k] for _l, _s, k, c in rel_claims), rel_claims
    assert {k for _l, _s, k, _c in rel_claims} == {
        "primitives",
        "verbs",
        "schemas",
        "error_codes",
        "regen",
    }, rel_claims


def test_stale_allowlist_check_fires_on_synthetic_entry(tmp_path: Path) -> None:
    """The stale-allowlist logic fires when an allowlisted substring is still
    present in a doc and now equals the live count."""
    counts = _live_counts()
    live_prims = counts["primitives"]
    doc = tmp_path / "note.md"
    substring = f"{live_prims} primitives"
    doc.write_text(f"# note\n\nWe have {substring}.\n", encoding="utf-8")
    masked = _mask(doc.read_text(encoding="utf-8"))
    # Present in the doc AND equal to live → the entry is stale.
    present = any(sub == substring for _l, sub, _k, _c in _count_claims(masked))
    assert present
    m = _COUNT_RE.search(substring)
    assert m is not None
    assert int(m.group(1)) == counts[_normalise_noun(m.group(2))]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
