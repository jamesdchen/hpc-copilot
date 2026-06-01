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

import os
import sys
from pathlib import Path

import yaml

# Regen scripts default to a core-only view — plugin-contributed primitives
# don't belong in this repo's docs/primitives/README.md catalog. Must precede
# the deferred hpc_agent import inside primitives_from_registry() (#198).
os.environ.setdefault("HPC_AGENT_DISABLE_PLUGINS", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _shared import REPO_ROOT, sort_verbs, summarize_side_effects  # noqa: E402

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
    # read → mutate → submit/build → workflow. Verbs not in the canonical
    # list fall to the end alphabetically — see scripts/_shared.sort_verbs.
    sorted_verbs = sort_verbs(list(by_verb.keys()))

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


def primitives_from_registry() -> list[dict]:
    """Project the @primitive registry into the dict shape this script
    already consumes (name / verb / idempotent / side_effects / backed_by).

    The registry is the SoT for behavior, structured side-effect kinds,
    and the ``cli`` invocation string. We still cross-reference each
    primitive's frontmatter for ``error_codes`` prose (per-code category /
    retry_safe / description fields the registry doesn't yet model).

    Falls through to pure-frontmatter parsing for any primitive missing
    from the registry, so the script stays useful during the C′
    migration window where some primitives might not be decorated yet.
    """
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from hpc_agent._kernel.registry.primitive import get_registry, register_primitives
    from hpc_agent.cli._dispatch import cli_to_invocation_string

    # The registry is now explicit: callers must register primitives
    # before querying. Without this call, get_registry() raises
    # RuntimeError. Idempotent — safe to call from anywhere.
    register_primitives()
    registered = get_registry()
    out: list[dict] = []
    seen: set[str] = set()
    for meta in registered.values():
        # Read frontmatter only for error_codes prose (the registry
        # carries the class refs but not the per-code prose fields yet).
        md_path = PRIMITIVES_DIR / f"{meta.name}.md"
        fm = parse_frontmatter(md_path) if md_path.is_file() else {}
        out.append(
            {
                "name": meta.name,
                "verb": meta.verb,
                "idempotent": bool(meta.idempotent),
                # Render structured registry SideEffects in the same
                # ``kind: target`` shape that summarize_side_effects
                # already understands; mixing dicts and strings stays
                # backward-compatible with frontmatter-only sources.
                "side_effects": [
                    {se.kind: se.target} if se.target else se.kind for se in meta.side_effects
                ],
                "error_codes": fm.get("error_codes", []),
                # backed_by.cli now comes from the registry; python is
                # derived from the func's qualified name. We still pass
                # the dict shape downstream consumers expect.
                "backed_by": {
                    "cli": (
                        cli_to_invocation_string(meta.name, meta.cli)
                        if meta.cli is not None
                        else "(none — Python-only primitive)"
                    ),
                    "python": f"{meta.func.__module__}.{meta.func.__qualname__}",
                },
            }
        )
        seen.add(meta.name)

    # Frontmatter holdouts (primitives present on disk but not yet
    # decorated). The registry switch is incremental; this fallback
    # keeps the catalog complete during the migration.
    for path in sorted(PRIMITIVES_DIR.glob("*.md")):
        if path.name == "README.md":
            continue
        fm = parse_frontmatter(path)
        if fm.get("name") in seen:
            continue
        out.append(fm)
    return out


def main() -> int:
    check = "--check" in sys.argv
    primitives = primitives_from_registry()
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
    README.parent.mkdir(parents=True, exist_ok=True)
    README.write_text(new, encoding="utf-8")
    print(f"regenerated catalog ({len(primitives)} primitives)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
