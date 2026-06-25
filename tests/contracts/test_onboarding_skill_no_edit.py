"""Onboarding skills must not carry the ``Edit`` tool.

Decoration is a deterministic verb (``decorate-entry-point``); free-form source
editing by the LLM was the affordance that let a worker rewrite a whole function
body instead of just adding the import + decorator. See
``docs/internals/engineering-principles.md`` — "The determinism boundary".
"""

from __future__ import annotations

from pathlib import Path

import pytest

_SKILLS = Path(__file__).resolve().parents[2] / "src" / "slash_commands" / "skills"
# Skills that touch the user's repo to onboard it — they wire entry points via
# verbs, never by free-form editing of user source.
_ONBOARDING_SKILLS = ["hpc-wrap-entry-point"]


def _allowed_tools(skill: str) -> list[str]:
    text = (_SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("allowed-tools:"):
            return line.split(":", 1)[1].split()
    raise AssertionError(f"{skill}: no allowed-tools frontmatter line")


@pytest.mark.parametrize("skill", _ONBOARDING_SKILLS)
def test_onboarding_skill_has_no_edit_tool(skill: str) -> None:
    tools = _allowed_tools(skill)
    assert "Edit" not in tools, (
        f"{skill} lists Edit in allowed-tools; onboarding skills must decorate via "
        "the decorate-entry-point verb, not free-form source editing."
    )
