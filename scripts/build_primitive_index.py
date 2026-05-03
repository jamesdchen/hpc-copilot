"""Regenerate the primitive catalog table in docs/primitives/README.md.

Parses YAML frontmatter from every docs/primitives/*.md (excluding
README.md), validates required fields, and writes a markdown table
between the BEGIN/END HTML-comment markers in README.md. Idempotent —
re-running with no frontmatter changes yields a no-op diff.

Usage:
    uv run python scripts/build_primitive_index.py [--check]

--check exits non-zero if the README is out of date (for CI).
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PRIMITIVES_DIR = REPO_ROOT / "docs" / "primitives"
README = PRIMITIVES_DIR / "README.md"
BEGIN = "<!-- BEGIN PRIMITIVE CATALOG -->"
END = "<!-- END PRIMITIVE CATALOG -->"
REQUIRED = ("name", "verb", "side_effects", "idempotent", "error_codes", "backed_by")


def parse_frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"{path.name}: missing YAML frontmatter")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise ValueError(f"{path.name}: unterminated YAML frontmatter")
    fm = yaml.safe_load(text[4:end])
    if not isinstance(fm, dict):
        raise ValueError(f"{path.name}: frontmatter is not a mapping")
    missing = [k for k in REQUIRED if k not in fm]
    if missing:
        raise ValueError(f"{path.name}: missing required frontmatter fields: {missing}")
    return fm


def summarize_side_effects(side_effects: list) -> str:
    """Compact one-line rendering for the catalog cell."""
    if not side_effects:
        return "_none_"
    parts: list[str] = []
    for entry in side_effects:
        if isinstance(entry, dict):
            for verb, target in entry.items():
                short_target = target.split(" ")[0].rstrip(",.;")
                parts.append(f"{verb}: `{short_target}`")
        else:
            parts.append(str(entry))
    return "; ".join(parts)


def render_table(primitives: list[dict]) -> str:
    """Render the catalog grouped by verb.

    The verb partitions primitives into bands the agent / reader can scan
    independently — query primitives are read-only and freely composable;
    mutation primitives need flock + idempotency key consideration; submit /
    scaffold primitives have unique side-effect classes; workflow primitives
    are end-to-end pipelines composed of the others.

    Within each verb band, primitives are sorted by name for stable diffs.
    """
    by_verb: dict[str, list[dict]] = {}
    for p in primitives:
        by_verb.setdefault(p["verb"], []).append(p)

    # Render verbs in the order that matches the architecture's tier story:
    # read → mutate → submit/build → workflow. Verbs not in this list fall
    # to the end alphabetically.
    verb_order = ["query", "validate", "mutate", "submit", "scaffold", "workflow"]
    sorted_verbs = sorted(
        by_verb.keys(),
        key=lambda v: (verb_order.index(v) if v in verb_order else len(verb_order), v),
    )

    sections: list[str] = []
    for verb in sorted_verbs:
        sections.append(f"\n### `{verb}` primitives\n")
        rows = ["| Primitive | Idempotent | Side effects | CLI |", "|---|---|---|---|"]
        for p in sorted(by_verb[verb], key=lambda x: x["name"]):
            cli = p["backed_by"].get("cli", "") if isinstance(p["backed_by"], dict) else ""
            rows.append(
                f"| [{p['name']}]({p['name']}.md) | "
                f"{'yes' if p['idempotent'] else 'no'} | "
                f"{summarize_side_effects(p['side_effects'])} | "
                f"`{cli}` |"
            )
        sections.append("\n".join(rows))
    return "\n".join(sections).strip()


def update_readme(table: str) -> tuple[str, str]:
    """Return (old_readme, new_readme). Caller writes if they differ."""
    old = README.read_text(encoding="utf-8")
    if BEGIN not in old or END not in old:
        raise ValueError(
            f"README.md missing markers '{BEGIN}' / '{END}'. "
            "Insert them around the catalog table before running this script."
        )
    pre, _, rest = old.partition(BEGIN)
    _, _, post = rest.partition(END)
    new = f"{pre}{BEGIN}\n{table}\n{END}{post}"
    return old, new


def main() -> int:
    check = "--check" in sys.argv
    primitives = []
    for path in sorted(PRIMITIVES_DIR.glob("*.md")):
        if path.name == "README.md":
            continue
        primitives.append(parse_frontmatter(path))

    table = render_table(primitives)
    old, new = update_readme(table)

    if old == new:
        print(f"catalog up to date ({len(primitives)} primitives)")
        return 0
    if check:
        print(
            "ERROR: catalog out of date — run scripts/build_primitive_index.py to regenerate",
            file=sys.stderr,
        )
        return 1
    README.write_text(new, encoding="utf-8")
    print(f"regenerated catalog ({len(primitives)} primitives)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
