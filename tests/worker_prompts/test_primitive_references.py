"""Cross-check ``hpc-agent <primitive>`` mentions in worker prompts.

A procedure that tells the worker to run ``hpc-agent foo`` where
``foo`` is not a real primitive sends the worker to a guaranteed
``unknown command`` error. The check is cheap: parse every
``hpc-agent X`` mention, resolve X against the live operations
catalog (plus a small allow-list of CLI verbs that aren't primitives
but are real subcommands).
"""

from __future__ import annotations

import re
from importlib.resources import files

import pytest

from hpc_agent._internal.operations import operations_catalog
from hpc_agent._schema_models.spawn_contract import WORKFLOW_PROCEDURES

# CLI verbs that are real subcommands but not @primitive-registered
# operations — agent_cli ships them outside the registry. Keep this
# list tight; every entry is a hand-maintained exception to the
# operations-catalog cross-check.
_CLI_VERBS_NOT_PRIMITIVES: frozenset[str] = frozenset(
    {
        "describe",
        "install-commands",
        "setup",
        "load-context",
        "capabilities",
        "run",
        # Aliases that the CLI exposes for primitives under a shorter name.
        "preflight",  # alias for check-preflight
        "status",  # alias for poll-run-status
        "submit",  # CLI shorthand used historically
        "aggregate",  # CLI shorthand used historically
        # Verb-group prefixes that take a subcommand.
        "clusters",
        "campaign",
    }
)


# Match ``hpc-agent <verb>`` only as a real invocation, never as a
# prose mention. A real invocation appears either inline-quoted
# (``\`hpc-agent foo\```) or at the start of a line in a fenced code
# block (optionally indented to align with a list item). A mention of
# the project name in prose ("hpc-agent has no kill primitive",
# "hpc-agent owns that via …") has neither.
_INVOCATION_RE = re.compile(
    r"(?:`|^[ \t]*)hpc-agent\s+([a-z][a-z0-9-]+)",
    re.MULTILINE,
)


def _procedure_text(workflow: str) -> str:
    return (files("hpc_agent.worker_prompts") / f"{workflow}.md").read_text(encoding="utf-8")


def _known_primitives() -> frozenset[str]:
    return frozenset(op["name"] for op in operations_catalog())


@pytest.mark.parametrize("workflow", sorted(WORKFLOW_PROCEDURES))
def test_procedure_only_references_known_primitives(workflow: str) -> None:
    """Every ``hpc-agent <verb>`` in the procedure must resolve."""
    text = _procedure_text(workflow)
    known = _known_primitives() | _CLI_VERBS_NOT_PRIMITIVES

    unknown: list[tuple[int, str]] = []
    for match in _INVOCATION_RE.finditer(text):
        verb = match.group(1)
        if verb not in known:
            line_no = text[: match.start()].count("\n") + 1
            unknown.append((line_no, verb))

    if unknown:
        bullets = "\n".join(f"  {workflow}.md:{ln}: hpc-agent {v}" for ln, v in unknown)
        raise AssertionError(
            f"{workflow}.md references unknown CLI verbs:\n{bullets}\n\n"
            "Either the primitive was renamed (update the procedure) or "
            "the procedure has a typo. If it's a real subcommand that "
            "isn't a @primitive, add it to _CLI_VERBS_NOT_PRIMITIVES in "
            "this test file."
        )
