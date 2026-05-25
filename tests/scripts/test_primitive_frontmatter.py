"""Smoke test that every docs/primitives/*.md has parseable frontmatter.

Replaces the deleted scripts/validate_primitive_contracts.py with cheap
coverage. Catches the "I forgot the closing ---" or YAML typo class of
bug without reintroducing the schema-vs-frontmatter drift problem (no
field-level cross-check; behavioral metadata only).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PRIM_DIR = REPO_ROOT / "docs" / "primitives"
REQUIRED_FIELDS = ("name", "verb", "side_effects", "idempotent", "error_codes", "backed_by")
ALLOWED_VERBS = ("query", "validate", "mutate", "submit", "scaffold", "workflow")


def _primitive_files() -> list[Path]:
    return [p for p in sorted(PRIM_DIR.glob("*.md")) if p.name != "README.md"]


def _parse_frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path.name}: missing YAML frontmatter opening"
    end = text.find("\n---\n", 4)
    assert end != -1, f"{path.name}: unterminated YAML frontmatter (no closing ---)"
    fm = yaml.safe_load(text[4:end])
    assert isinstance(fm, dict), f"{path.name}: frontmatter is not a mapping"
    return fm


@pytest.mark.parametrize("path", _primitive_files(), ids=lambda p: p.stem)
def test_frontmatter_parses(path: Path) -> None:
    """Frontmatter is well-formed YAML and carries the required fields."""
    fm = _parse_frontmatter(path)
    missing = [k for k in REQUIRED_FIELDS if k not in fm]
    assert not missing, f"{path.name}: missing required frontmatter fields: {missing}"
    assert fm["verb"] in ALLOWED_VERBS, f"{path.name}: verb {fm['verb']!r} not in {ALLOWED_VERBS}"
    assert fm["name"] == path.stem, (
        f"{path.name}: frontmatter name {fm['name']!r} != filename stem {path.stem!r}"
    )


def test_at_least_one_primitive_per_verb_band() -> None:
    """Sanity: the catalog is non-trivial in each major band."""
    verbs = {_parse_frontmatter(p)["verb"] for p in _primitive_files()}
    # Workflow + at least one of (query / mutate / submit / scaffold) must exist.
    assert "workflow" in verbs, "no workflow-tier primitives — composite layer empty?"
    assert verbs & {"query", "mutate", "submit", "scaffold"}, "no leaf primitives at all"


def test_every_registered_primitive_has_a_doc() -> None:
    """Every name in the @primitive registry must have a docs/primitives/<name>.md.

    Catches the silent-orphan case where someone adds @primitive(...) but
    forgets to create the doc file. ``scripts/build_primitive_frontmatter.py
    --write`` auto-scaffolds missing docs; this test fails CI when the
    scaffold step was skipped.

    Filters to core-only primitives — ``docs/primitives/`` ships with the
    core package and plugins (``hpc-agent-pro``) own their own per-plugin
    docs trees. The filter is a no-op when no plugin is installed.
    """
    from tests._registry_helpers import core_only_registry

    docs = {p.stem for p in _primitive_files()}
    missing = sorted(name for name in core_only_registry() if name not in docs)
    assert not missing, (
        "primitives in the @primitive registry have no docs/primitives/<name>.md: "
        f"{missing}. Run scripts/build_primitive_frontmatter.py --write to scaffold."
    )
