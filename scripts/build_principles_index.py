"""Regenerate the section index in docs/internals/engineering-principles.md.

The engineering-principles page is an INDEX plus the two judgment rules kept
in full; every other section lives as a self-contained file under
``docs/internals/principles/<slug>.md`` (prose + its own enforcement map +
drift log). This script rebuilds the section-listing table between the
``BEGIN/END GENERATED SECTION INDEX`` markers from each section file's YAML
frontmatter (``slug`` / ``order`` / ``title`` / ``scope``) and a token-ish
size derived from the file's byte length. It never touches the static
preamble or the judgment rules.

A ``regen_all`` step. Invoke exactly two ways::

    python scripts/build_principles_index.py --check   # gate: report drift
    python scripts/build_principles_index.py --write   # apply: rewrite listing

Bare invocation is refused (rc 2): this generator has no safe default mode.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PRINCIPLES_DIR = REPO_ROOT / "docs" / "internals" / "principles"
INDEX = REPO_ROOT / "docs" / "internals" / "engineering-principles.md"

BEGIN = "<!-- BEGIN GENERATED SECTION INDEX -->"
END = "<!-- END GENERATED SECTION INDEX -->"

REQUIRED_FIELDS = ("slug", "order", "title", "scope")


def parse_frontmatter(path: Path) -> dict:
    """Parse and validate a section file's YAML frontmatter."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"{path.name}: missing YAML frontmatter")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise ValueError(f"{path.name}: unterminated YAML frontmatter")
    fm = yaml.safe_load(text[4:end])
    if not isinstance(fm, dict):
        raise ValueError(f"{path.name}: frontmatter is not a mapping")
    missing = [k for k in REQUIRED_FIELDS if k not in fm]
    if missing:
        raise ValueError(f"{path.name}: missing required frontmatter fields: {missing}")
    if fm["slug"] != path.stem:
        raise ValueError(f"{path.name}: slug '{fm['slug']}' != filename stem '{path.stem}'")
    return fm


def _token_ish(path: Path) -> str:
    """A stable, coarse size hook: ~N.Nk tokens (bytes/4, rounded to 0.1k).

    Coarse on purpose — the listing advertises rough weight, so an edit that
    doesn't cross a bucket boundary needs no index regen, while a title/scope
    change (the thing this index actually pins) always moves a cell.
    """
    chars = len(path.read_text(encoding="utf-8"))
    k = round(chars / 4 / 100) / 10  # nearest 0.1k tokens
    return f"~{k:.1f}k tokens"


def load_sections(sections_dir: Path = PRINCIPLES_DIR) -> list[dict]:
    """Every section file, validated and sorted by ``order`` (then slug).

    A duplicate ``order`` is refused — the listing order must be total.
    """
    sections: list[dict] = []
    for path in sorted(sections_dir.glob("*.md")):
        fm = parse_frontmatter(path)
        fm["_size"] = _token_ish(path)
        sections.append(fm)
    orders = [s["order"] for s in sections]
    if len(set(orders)) != len(orders):
        raise ValueError(f"duplicate 'order' among section files: {sorted(orders)}")
    sections.sort(key=lambda s: (s["order"], s["slug"]))
    return sections


def render_table(sections: list[dict]) -> str:
    """The section-listing markdown table."""
    rows = ["| Section | Scope | Size |", "|---|---|---|"]
    for s in sections:
        rows.append(f"| [{s['title']}](principles/{s['slug']}.md) | {s['scope']} | {s['_size']} |")
    return "\n".join(rows)


def splice_index(old: str, table: str) -> str:
    """Return the index text with the marker block replaced by *table*."""
    if BEGIN not in old or END not in old:
        raise ValueError(f"{INDEX.name} missing markers '{BEGIN}' / '{END}'")
    pre, _, rest = old.partition(BEGIN)
    _, _, post = rest.partition(END)
    return f"{pre}{BEGIN}\n{table}\n{END}{post}"


def build(*, sections_dir: Path = PRINCIPLES_DIR, index_path: Path = INDEX, write: bool) -> int:
    sections = load_sections(sections_dir)
    table = render_table(sections)
    old = index_path.read_text(encoding="utf-8")
    new = splice_index(old, table)
    if old == new:
        print(f"principles index up to date ({len(sections)} sections)")
        return 0
    if not write:
        print(
            "ERROR: principles index out of date — run "
            "'python scripts/build_principles_index.py --write' to regenerate",
            file=sys.stderr,
        )
        return 1
    index_path.write_text(new, encoding="utf-8")
    print(f"regenerated principles index ({len(sections)} sections)")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    check = "--check" in args
    write = "--write" in args
    if check == write:  # neither, or both — refuse
        print(
            "usage: python scripts/build_principles_index.py --check | --write\n"
            "  --check  gate: rebuild-and-compare the section listing, report drift\n"
            "  --write  apply: rewrite the section listing in place",
            file=sys.stderr,
        )
        return 2
    return build(write=write)


if __name__ == "__main__":
    sys.exit(main())
