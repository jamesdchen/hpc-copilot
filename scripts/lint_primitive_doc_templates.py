"""Lint that ``docs/primitives/<name>.md`` body templates match each
primitive's ``agent_facing`` flag.

The two audiences expect different content. Agent-facing primitives
follow an outward-looking template (``# Inputs`` / ``# Outputs`` /
``# Errors`` / ``# Idempotency`` / ``# Notes``) — the LLM reads
``capabilities --full`` and follows hyperlinks from slash commands.
Internal primitives follow a contributor-looking template (``#
Composers`` / ``# Invariants`` / ``# Coupling`` / ``# Failure
modes``) — the audience is the next person maintaining the
framework.

This lint catches the case where the two drift apart: an internal
atom written with agent-facing prose (which won't appear in
``capabilities --full`` so the prose is wasted) or an agent-facing
atom written with contributor prose (which leaves an integrating
agent without input/output guidance).

Heuristic: count agent-facing section headers vs. internal-template
section headers; the larger group should match the
``agent_facing`` flag. A doc with neither set of headers is a stub
(deliberately allowed for primitives still being authored).

Usage::

    uv run python scripts/lint_primitive_doc_templates.py            # report
    uv run python scripts/lint_primitive_doc_templates.py --strict   # also exit 1 on stubs
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PRIM_DIR = REPO_ROOT / "docs" / "primitives"

# Section headers that signal each template. Substring match (case-
# insensitive) on lines starting with ``## ``.
AGENT_FACING_HEADERS = (
    "inputs",
    "outputs",
    "errors",
    "idempotency",
    "usage",
)
INTERNAL_HEADERS = (
    "composers",
    "invariants",
    "coupling",
    "failure modes",
)

_HEADER_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def _classify_body(body: str) -> tuple[int, int]:
    """Return ``(agent_facing_score, internal_score)`` from section headers."""
    agent = 0
    internal = 0
    for match in _HEADER_RE.finditer(body):
        header = match.group(1).strip().lower()
        if any(h in header for h in AGENT_FACING_HEADERS):
            agent += 1
        if any(h in header for h in INTERNAL_HEADERS):
            internal += 1
    return agent, internal


def _is_stub(body: str) -> bool:
    return "_Documentation pending._" in body or len(body.strip().splitlines()) <= 3


def _strip_frontmatter(text: str) -> str:
    return _FRONTMATTER_RE.sub("", text, count=1)


def main() -> int:
    strict = "--strict" in sys.argv

    sys.path.insert(0, str(REPO_ROOT / "src"))
    from hpc_agent._internal.primitive import get_registry, register_primitives

    register_primitives()
    registry = get_registry()

    misalignments: list[str] = []
    stubs: list[str] = []

    for path in sorted(PRIM_DIR.glob("*.md")):
        if path.name == "README.md":
            continue
        name = path.stem
        meta = registry.get(name)
        if meta is None:
            # Doc exists but no primitive — orphan. Out of scope here;
            # ``test_every_registered_primitive_has_a_doc`` covers
            # the inverse direction.
            continue
        body = _strip_frontmatter(path.read_text(encoding="utf-8"))
        if _is_stub(body):
            stubs.append(name)
            continue

        agent_score, internal_score = _classify_body(body)
        # Tie / unsigned cases are lenient — the doc has structure
        # that doesn't match either template (e.g. a single ``# Notes``
        # section). We only complain when the doc clearly leans toward
        # the wrong template for its tier.
        leaning_internal = internal_score > agent_score
        leaning_agent = agent_score > internal_score
        if meta.agent_facing and leaning_internal:
            misalignments.append(
                f"{name}: agent_facing=True but body uses contributor template "
                f"(internal_headers={internal_score}, agent_headers={agent_score}). "
                f"Either flip agent_facing=False or rewrite the body in the "
                f"Inputs/Outputs/Errors/Idempotency template."
            )
        elif (not meta.agent_facing) and leaning_agent:
            misalignments.append(
                f"{name}: agent_facing=False but body uses agent-facing template "
                f"(agent_headers={agent_score}, internal_headers={internal_score}). "
                f"Either flip agent_facing=True or rewrite the body in the "
                f"Composers/Invariants/Coupling template (the LLM never sees "
                f"this body since render_llms_full skips internal primitives)."
            )

    rc = 0
    if misalignments:
        print(f"ERROR: {len(misalignments)} doc/agent_facing template mismatch(es):")
        for m in misalignments:
            print(f"  - {m}")
        rc = 1
    if stubs and strict:
        print(f"ERROR: {len(stubs)} stub doc(s) (--strict mode):")
        for n in stubs:
            print(f"  - {n}")
        rc = 1
    elif stubs:
        print(f"note: {len(stubs)} primitive(s) have stub bodies (allowed): {stubs}")

    if rc == 0:
        print(f"docs aligned with agent_facing partition ({len(registry)} primitives)")
    return rc


if __name__ == "__main__":
    sys.exit(main())
