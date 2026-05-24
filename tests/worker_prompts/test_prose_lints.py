"""Prose-quality lints for the inlined worker-prompt procedures.

Tolerable in an LLM-discovered chat skill: ambiguity, hedging, "if
needed." Not tolerable here: the worker is headless, headed for a
single shot, and the prose IS the contract. These lints enforce the
imperative tone the spawn pipeline depends on.
"""

from __future__ import annotations

import re
from importlib.resources import files

import pytest

from hpc_agent._wire.spawn_contract import WORKFLOW_PROCEDURES

# Phrases that signal hedging or ask the worker to make a judgement call
# where the procedure should be telling it what to do. Each is a *word
# boundary* match — substring is too aggressive ("considered" ≠
# "consider").
_BANNED_PHRASES: tuple[str, ...] = (
    r"\btry to\b",
    r"\bif needed\b",
    r"\bif desired\b",
    r"\bshould probably\b",
    r"\bmight want to\b",
    r"\bmay want to\b",
    r"\bfeel free to\b",
)


def _procedure_text(workflow: str) -> str:
    """The host's procedure body for *workflow* as a string."""
    return (files("hpc_agent._kernel.extension.worker_prompts") / f"{workflow}.md").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize("workflow", sorted(WORKFLOW_PROCEDURES))
@pytest.mark.parametrize("phrase", _BANNED_PHRASES)
def test_procedure_avoids_hedging_phrase(workflow: str, phrase: str) -> None:
    """Hedging phrases produce non-deterministic worker behavior."""
    text = _procedure_text(workflow)
    match = re.search(phrase, text, re.IGNORECASE)
    if match is not None:
        line_no = text[: match.start()].count("\n") + 1
        raise AssertionError(
            f"{workflow}.md:{line_no} contains banned hedging phrase "
            f"{phrase!r}. A worker prompt is deterministic — replace "
            "with an imperative ('do X', 'record Y in decisions') or "
            "make the branch explicit."
        )


@pytest.mark.parametrize("workflow", sorted(WORKFLOW_PROCEDURES))
def test_procedure_has_no_frontmatter(workflow: str) -> None:
    """Procedures are inert text; frontmatter is a skill-mechanism leftover."""
    text = _procedure_text(workflow)
    assert not text.startswith("---\n"), (
        f"{workflow}.md begins with YAML frontmatter; worker-prompt "
        "templates are inlined verbatim. Strip the frontmatter."
    )


@pytest.mark.parametrize("workflow", sorted(WORKFLOW_PROCEDURES))
def test_procedure_is_nonempty(workflow: str) -> None:
    """A procedure must have content; an empty file silently breaks workers."""
    text = _procedure_text(workflow).strip()
    assert len(text) > 100, f"{workflow}.md is under 100 chars; likely empty or stub"
