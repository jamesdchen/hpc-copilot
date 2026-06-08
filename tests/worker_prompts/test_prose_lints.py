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


# Agent-facing safety-bypass fields that have been demoted to operator/internal
# control: an agent must NOT be taught to set any of these true in a spec
# example. Each silenced or skipped a real cluster-side safety check —
# ``skip_preflight`` silenced the ``command -v uv`` runtime probe (#275) and
# ``skip_rsync_deploy`` dropped the rsync+deploy so a stale local tree ran on
# old cluster code (#185/#283). The fix wasn't a prose warning — it was taking
# the lever off the wire (operator-only ``HPC_*`` env var + a refused spec
# field). This lint guards the CLASS going forward: a new bypass field someone
# adds to a worker-prompt example as ``<field>: true`` fails CI here, the same
# way #275/#283 refused the field at the wire.
_BYPASS_FIELDS: tuple[str, ...] = (
    "skip_preflight",
    "skip_rsync_deploy",
)

# Match an EXAMPLE that SETS one of the bypass fields true — i.e. an assignment
# form (``field: true``, ``"field": true``, ``field=true``), NOT the negative
# prose the demotion notes use ("there is no longer a `skip_preflight` field",
# "There is **no `skip_preflight`**"). The assignment shape is what would teach
# an agent to author the lever; the prose mentions are the documentation of its
# removal and must stay legal.
_BYPASS_SET_TRUE_RE = re.compile(
    r"""["'`]?                       # optional opening quote/backtick
        (skip_preflight|skip_rsync_deploy)
        ["'`]?                       # optional closing quote/backtick
        \s*[:=]\s*                   # JSON ':' or kwarg '='
        ["'`]?true                   # the value true (optionally quoted)
    """,
    re.IGNORECASE | re.VERBOSE,
)


@pytest.mark.parametrize("workflow", sorted(WORKFLOW_PROCEDURES))
def test_procedure_does_not_teach_a_safety_bypass_field(workflow: str) -> None:
    """No worker-prompt example may instruct setting a preflight/deploy bypass
    field true (#283). ``skip_preflight`` (#275) and ``skip_rsync_deploy``
    (#185/#283) each disabled a real cluster-side safety check and were demoted
    to operator-only ``HPC_*`` env vars / refused at the wire. An agent example
    that sets one true would re-create the bug surface the demotion closed; this
    lint catches the whole class — including any new bypass field added to
    ``_BYPASS_FIELDS`` — before it ships.
    """
    text = _procedure_text(workflow)
    match = _BYPASS_SET_TRUE_RE.search(text)
    if match is not None:
        line_no = text[: match.start()].count("\n") + 1
        field = match.group(1)
        raise AssertionError(
            f"{workflow}.md:{line_no} teaches an agent to set the safety-bypass "
            f"field {field!r} true ({match.group(0)!r}). That lever was demoted "
            "to operator/internal-only control (an HPC_* env var + a wire-refused "
            "spec field) precisely because an agent example like this silenced a "
            "cluster-side safety check (#275 / #185 / #283). Remove the example; "
            "the skip is the operator's call, not the agent's."
        )
