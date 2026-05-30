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

from hpc_agent._wire.spawn_contract import DECISION_POINTS, WORKFLOW_PROCEDURES

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


# Prose that names a `decisions` point ID. Two shapes the procedures use:
#   - ``record a `<point>` decision``  (the canonical imperative)
#   - ``record `<point>` in `decisions```  (older phrasing)
# Both bind a backtick-quoted token to the strict ``decisions`` record, so the
# token MUST be in DECISION_POINTS[workflow] or parse_worker_report rejects the
# envelope and the run reports broken even on success (#183 / #194). This lint
# turns that class from "caught in a live demo" into "caught by CI".
_DECISION_REF_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"record(?:s|ed)?\s+(?:a|an)\s+`([a-z_][a-z0-9_]*)`\s+decision", re.IGNORECASE),
    re.compile(r"record(?:s|ed)?\s+`([a-z_][a-z0-9_]*)`\s+in\s+`decisions`", re.IGNORECASE),
)


@pytest.mark.parametrize("workflow", sorted(WORKFLOW_PROCEDURES))
def test_decisions_point_ids_are_in_the_allowlist(workflow: str) -> None:
    """Every `point` ID a procedure tells the worker to record in `decisions`
    must be in that workflow's DECISION_POINTS allowlist (#194). A token outside
    it is rejected by parse_worker_report — the #183 failure class."""
    text = _procedure_text(workflow)
    allowed = {p.id for p in DECISION_POINTS.get(workflow, ())}
    referenced: set[str] = set()
    for pattern in _DECISION_REF_RES:
        referenced.update(m.group(1) for m in pattern.finditer(text))
    unknown = sorted(referenced - allowed)
    assert not unknown, (
        f"{workflow}.md instructs the worker to record decision point(s) "
        f"{unknown} that are NOT in DECISION_POINTS[{workflow!r}] "
        f"({sorted(allowed)}). parse_worker_report would reject the envelope. "
        "Map each to an allowed point ID with a descriptive `outcome`, and route "
        "free-form detail to `anomalies` (see the Reporting conventions section)."
    )
