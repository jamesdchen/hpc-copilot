#!/usr/bin/env python3
"""Every verb a SKILL body names as MCP-direct must be curated-reachable.

The SKILLs instruct the agent to call certain verbs "DIRECT through MCP — never
a spec-file round-trip" (hpc-submit / hpc-status / hpc-aggregate / hpc-campaign /
hpc-notebook-audit), and tag others inline ``(MCP-direct)`` (``revise-resolved``,
``retarget-run``). That instruction only holds if the named verb is actually
exposed by the MCP server's **curated** catalog — the surface those workflows run
against. When it is NOT, the agent drops to the CLI and HAND-ROLLS the call: the
run-#8 unreachable-verb class (a Write + Bash + Read spec-file round-trip for a
value one MCP call returns; for a mutating verb, a hand-authored spec JSON — the
finding-4/10/13/17 corruption source). This lint mechanizes the coupling the
architect memo §1 named: the enforcement is an affordance/lint, never SKILL
prose the agent can drift from.

Reachability is DERIVED, never hardcoded: a verb is reachable iff it is in the
catalog **that skill's consumer actually runs against** — the live
:class:`hpc_agent._kernel.extension.mcp_server.McpServer` projection. Most SKILLs
target ``catalog="curated"`` (the human-amplification surface: the derived
``next_block`` blocks unioned with
:data:`~hpc_agent._kernel.extension.mcp_server._CURATED_EXTRA_VERBS`). A SKILL may
declare a DIFFERENT catalog in its frontmatter — ``mcp-catalog: <name>`` — when it
is written for a different consumer: the Claude-Science producer skill declares
``mcp-catalog: science`` and is checked against the narrower ``science`` catalog
(``queue-run`` + ``queue-status`` + ``queue-advance``), NOT curated. Checking a
producer skill against curated would be the wrong question — its consumer never
sees the curated catalog. So the fix for a violation is to make the verb reachable
in the SKILL's own catalog (a ``_CURATED_EXTRA_VERBS`` entry / a ``next_block``
field for a curated skill; membership of ``_SCIENCE_VERBS`` for the science skill),
or to stop calling it MCP-direct in the SKILL — never to edit this lint.

Detection (:func:`find_mcp_direct_verbs`), two markers, both requiring a
backtick-quoted verb token (``^[a-z][a-z0-9-]*$``):

1. **Inline tag** — a backtick token immediately followed by a parenthetical
   naming MCP + direct, e.g. ``\\`revise-resolved\\` (MCP-direct)`` or
   ``\\`attention-queue\\` (read-only MCP, direct — …)``.
2. **The DIRECT-through-MCP enumeration** — any line containing the phrase
   "DIRECT through MCP"; every backtick-quoted verb token on that line is taken
   as MCP-direct-named. (This is the "Read-only QUERY verbs go DIRECT through
   MCP" bullet, whose whole purpose is to enumerate the MCP-direct reads. Putting
   a NON-MCP-direct verb inside that enumeration is itself the documentation bug
   this lint rightly flags — keep the CLI-only fallback prose out of the bullet.)

Every captured token is filtered to the live registry before the reachability
check (:func:`check`), so non-verb backtick tokens that pass the token regex but
are not primitives — ``hpc-agent``, an example flag — are ignored; a genuine
typo'd verb name is simply not a registry verb and out of scope here (the
skill/command-sync lints own dangling references).

Usage::

    uv run python scripts/lint_skill_mcp_reachability.py
"""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "src" / "hpc_agent" / "slash_commands" / "skills"

# A verb token: kebab-case, leading letter — mirrors the ``describe`` name rule
# and the skill_returns._SKILL_NAME_RE charset. Wrapped in backticks in the SKILL
# body. The trailing anchor is enforced by the capture (a full backtick span).
_VERB_TOKEN_RE = re.compile(r"^[a-z][a-z0-9-]*$")

# A backtick-quoted span (no embedded backtick). Group 1 is the inner text; a
# span carrying spaces/slashes/dots (``hpc-agent <verb> --spec …``,
# ``.hpc/specs/*.json``) simply fails ``_VERB_TOKEN_RE`` and is skipped. Scanned
# PER LINE, not over the whole document: a ``` code fence (an odd backtick run)
# would otherwise shift every subsequent open/close pairing by one, mispairing a
# closing backtick with the next span's opening one. Per-line, inline code is
# balanced (verified: every SKILL line's backtick count is even), so the pairing
# is stable; fenced blocks are skipped outright.
_BACKTICK_SPAN_RE = re.compile(r"`([^`]+)`")

# A ``` fence toggle line (``` or ```lang). Lines inside a fenced block carry
# example code, not MCP-direct directives, so they are skipped.
_FENCE_RE = re.compile(r"^\s*```")

# The DIRECT-through-MCP enumeration marker (case-insensitive): the "Read-only
# QUERY verbs go DIRECT through MCP — never a spec-file round-trip" bullet.
_DIRECT_PHRASE_RE = re.compile(r"direct through mcp", re.IGNORECASE)

# A SKILL's frontmatter ``mcp-catalog: <name>`` declaration — the MCP catalog its
# consumer runs against. Absent ⇒ the default ``curated`` (the human-amplification
# surface every workflow skill uses). A producer skill written for a different
# consumer (Claude Science) declares its own catalog so it is checked against the
# RIGHT surface, not curated.
_MCP_CATALOG_RE = re.compile(r"^mcp-catalog:\s*([a-z][a-z0-9-]*)\s*$", re.MULTILINE)
_DEFAULT_CATALOG = "curated"


def _declared_catalog(skill_text: str) -> str:
    """The catalog a SKILL's consumer runs against (frontmatter ``mcp-catalog:``,
    default :data:`_DEFAULT_CATALOG`)."""
    match = _MCP_CATALOG_RE.search(skill_text)
    return match.group(1) if match else _DEFAULT_CATALOG


# An inline ``(MCP-direct)`` / ``(read-only MCP, direct — …)`` parenthetical: the
# parens must mention BOTH "mcp" and "direct". Matched right after a backtick
# span (a small gap tolerates ``\\`retarget-run\\` (MCP-direct)`` where a word may
# sit between — but the parenthetical must open within a short window).
_INLINE_TAG_RE = re.compile(r"\((?=[^)]*\bmcp\b)(?=[^)]*\bdirect\b)[^)]*\)", re.IGNORECASE)
# The window (chars) between a backtick span's close and the opening ``(`` of an
# MCP-direct parenthetical for the tag to bind to that token. Generous enough for
# a short connector ("with", "using") but not a whole clause.
_INLINE_TAG_WINDOW = 8


def find_mcp_direct_verbs(skill_text: str) -> set[str]:
    """Verb tokens *skill_text* names as MCP-direct (pure; no registry check).

    Returns the union of the two markers documented in the module docstring. The
    tokens are raw (backtick-inner, verb-shaped) strings — the caller
    (:func:`check`) filters them to the live registry before requiring
    reachability, so this stays a pure text scan with no import of the registry.
    """
    found: set[str] = set()
    in_fence = False
    for line in skill_text.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        direct_line = _DIRECT_PHRASE_RE.search(line) is not None
        for span in _BACKTICK_SPAN_RE.finditer(line):
            token = span.group(1)
            if not _VERB_TOKEN_RE.match(token):
                continue
            # Marker 2: every verb token listed alongside the DIRECT-through-MCP
            # phrase on this line.
            if direct_line:
                found.add(token)
                continue
            # Marker 1: an inline ``(MCP-direct)`` tag opening just after the
            # token (within a short window that tolerates a one-word connector).
            tail = line[span.end() : span.end() + _INLINE_TAG_WINDOW]
            paren = tail.find("(")
            if paren >= 0 and _INLINE_TAG_RE.match(line[span.end() + paren :]):
                found.add(token)
    return found


def check(
    skill_texts: Mapping[str, str],
    registry_verbs: set[str],
    curated_verbs: set[str],
    *,
    catalog_verbs: Mapping[str, set[str]] | None = None,
) -> list[str]:
    """Return one error string per (skill, verb) MCP-direct-but-unreachable pair.

    Pure: takes the SKILL bodies keyed by a display label, the set of live
    registry verb names (to filter non-verb backtick tokens out), and the set of
    curated-reachable verb names. Each SKILL is checked against the catalog its
    OWN consumer runs against: a SKILL declaring ``mcp-catalog: <name>`` in its
    frontmatter is checked against ``catalog_verbs[<name>]`` (e.g. the narrower
    ``science`` producer catalog); one that declares nothing defaults to
    ``curated`` (``curated_verbs``). A captured token is a violation iff it IS a
    registry verb (a real primitive the SKILL names MCP-direct) but is NOT in that
    SKILL's catalog. Empty list ⇒ clean.

    ``curated_verbs`` stays a positional argument (curated is the default surface,
    and the pure unit tests inject it directly); ``catalog_verbs`` is an additive
    map for the non-default catalogs (``{"science": {...}}``) — the ``curated``
    entry is always the ``curated_verbs`` set regardless of what the map carries.
    """
    catalogs: dict[str, set[str]] = dict(catalog_verbs or {})
    catalogs[_DEFAULT_CATALOG] = curated_verbs
    errors: list[str] = []
    for label in sorted(skill_texts):
        catalog_name = _declared_catalog(skill_texts[label])
        reachable = catalogs.get(catalog_name, curated_verbs)
        named = find_mcp_direct_verbs(skill_texts[label])
        for verb in sorted(named):
            if verb not in registry_verbs:
                continue  # not a primitive — a path/flag/example token, out of scope
            if verb not in reachable:
                fix = (
                    "add a _CURATED_EXTRA_VERBS entry in "
                    "src/hpc_agent/_kernel/extension/mcp_server.py (or a next_block "
                    "field if it is truly a block)"
                    if catalog_name == _DEFAULT_CATALOG
                    else f"make it a member of the {catalog_name!r} catalog"
                )
                errors.append(
                    f"{label}: names {verb!r} as MCP-direct, but it is NOT reachable "
                    f"from the {catalog_name!r} MCP catalog (the surface this SKILL's "
                    "consumer runs against). An MCP-direct verb the catalog does not "
                    "expose forces the agent to hand-roll the call (the run-#8 "
                    "unreachable-verb class: a spec-file round-trip, or a "
                    f"hand-authored spec JSON for a mutate). Fix: {fix}, or stop "
                    "calling it MCP-direct in the SKILL."
                )
    return errors


def _load_skill_texts() -> dict[str, str]:
    """Every ``skills/*/SKILL.md`` body, keyed by its repo-relative path."""
    out: dict[str, str] = {}
    for path in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        out[str(path.relative_to(REPO_ROOT))] = path.read_text(encoding="utf-8")
    return out


def _live_registry_and_catalogs() -> tuple[set[str], set[str], dict[str, set[str]]]:
    """The live registry verb names, the curated-reachable set, and the per-catalog
    reachable sets for the non-default catalogs a SKILL may declare.

    Imports lazily so a pure ``find_mcp_direct_verbs`` / ``check`` unit test need
    not construct the server. Every catalog's reachability is read off the same
    ``McpServer`` the ``mcp-serve`` daemon runs, so the lint can never disagree
    with the surface the agent actually calls. The ``science`` catalog is built
    with ``allow_mutations=True`` because that is how its consumer (Claude Science)
    registers it — ``hpc-agent mcp-serve --catalog science --allow-mutations`` —
    so ``queue-run`` (a ``mutate``) is genuinely reachable there.
    """
    from hpc_agent._kernel.extension.mcp_server import build_server
    from hpc_agent._kernel.registry.primitive import get_registry, register_primitives

    register_primitives()
    registry_verbs = set(get_registry())
    curated = set(build_server(catalog="curated")._curated_names())
    science = set(build_server(catalog="science", allow_mutations=True)._science_names())
    return registry_verbs, curated, {"science": science}


def main() -> int:
    registry_verbs, curated, catalog_verbs = _live_registry_and_catalogs()
    errors = check(_load_skill_texts(), registry_verbs, curated, catalog_verbs=catalog_verbs)
    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1
    print("all MCP-direct verbs named in SKILLs are curated-reachable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
