"""Contract: the interview skill elicits authorship-locked fields as FREE-TEXT.

The human-authorship gate (``ops/decision/journal._assert_human_authorship``)
refuses committing a ``REQUIRED_CALLER_FIELDS`` value whose tokens do not appear
in the human's own utterance log. If the interview skill offers such a field as a
pre-filled ``AskUserQuestion`` option, a click carries no authorship and the gate
refuses it at ``append-decision`` — the run #7 awkwardness where the tool asks a
multiple-choice question and then rejects the answer, forcing a re-type.

This binds the SAME ``REQUIRED_CALLER_FIELDS`` partition the gate consults to the
ELICITATION surface, so the two can never contradict: every locked field must be
documented as free-text elicitation in the wrap-entry-point skill. Add a field to
the partition without documenting its free-text elicitation and this test fires.
"""

from __future__ import annotations

import re
from pathlib import Path

from hpc_agent.ops.submit.field_partition import REQUIRED_CALLER_FIELDS

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WRAP_SKILL = _REPO_ROOT / "src/hpc_agent/slash_commands/skills/hpc-wrap-entry-point/SKILL.md"

# The distinctive sentence carrying the rule — kept in lockstep with the SKILL.
_MARKER = "carries no authorship"


def _paragraph_with(text: str, needle: str) -> str:
    """Return the blank-line-delimited paragraph containing *needle* (or '')."""
    for para in text.split("\n\n"):
        if needle in para:
            return para
    return ""


def test_wrap_skill_elicits_required_caller_fields_as_free_text() -> None:
    text = _WRAP_SKILL.read_text(encoding="utf-8")
    para = _paragraph_with(text, _MARKER)
    assert para, (
        f"hpc-wrap-entry-point SKILL.md must document the authorship-elicitation rule "
        f"(marker {_MARKER!r}) — the free-text complement to the human-authorship gate"
    )
    assert re.search(r"free-?text", para, re.IGNORECASE), (
        "the rule must state the locked fields are elicited as FREE-TEXT the human types"
    )
    assert re.search(r"pre-?fill|click|button|option", para, re.IGNORECASE), (
        "the rule must call out that a click on a pre-filled option is authorless"
    )
    for field in sorted(REQUIRED_CALLER_FIELDS):
        assert field in para, (
            f"{field!r} is a REQUIRED_CALLER field but is not named in the free-text "
            f"elicitation rule — the partition drifted from the interview guidance"
        )
