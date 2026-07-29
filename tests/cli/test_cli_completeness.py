"""Pin: every agent-facing primitive has a CLI subcommand.

The CLI surface and the ``@primitive`` registry are two of three
sources of truth for primitive contracts (the third is the JSON schema
in ``hpc_agent/schemas/``). When they drift — a primitive lands
without a CLI subparser registration — agents and tests silently lose
access to it.

This test walks the operations catalog for ``agent_facing == True``
primitives and asserts each has a matching argparse subcommand. The
mapping primitive-name → CLI-verb is mostly identity (``submit-flow``
↔ ``submit-flow``); the small set of historical aliases lives in
``_PRIMITIVE_TO_CLI_VERB``.

Skipping this gate means a future registry-driven dispatcher couldn't
trust the mapping it's about to collapse. Until that dispatcher lands
(see ``docs/internals/skill-policy.md`` and the planning doc on the
registry-driven CLI future), this is the cheap insurance.
"""

from __future__ import annotations

import pytest

from hpc_agent.cli.dispatch import build_parser
from tests._registry_helpers import core_only_operations_catalog

# Primitives whose CLI verb differs from their catalog name. Each entry
# is a deliberate alias documented at the relevant adapter.
_PRIMITIVE_TO_CLI_VERB: dict[str, str] = {
    "poll-run-status": "status",
    "submit-spec": "submit",
    "combine-wave": "aggregate",
    "check-preflight": "preflight",
    "reconcile-journal": "reconcile",
    "resubmit-failed": "resubmit",
    # Verb-group children — space-separated form is what `_live_subcommands`
    # surfaces for nested subparsers (``hpc-agent campaign init`` etc.).
    "campaign-init": "campaign init",
    "campaign-list": "campaign list",
    "campaign-status": "campaign status",
    "campaign-replay": "campaign replay",
    "campaign-converged": "campaign converged",
    "campaign-budget": "campaign budget",
    "campaign-advance": "campaign advance",
    "campaign-acknowledge-budget": "campaign acknowledge-budget",
    "campaign-health": "campaign health",
    "clusters-list": "clusters list",
    "clusters-describe": "clusters describe",
    "recoveries-list": "recoveries list",
    "recoveries-show": "recoveries show",
}

# NOTE: there is deliberately no ``_INTENTIONALLY_NO_CLI`` exemption set.
#
# It used to hold eleven names and it was an artifact of a contradiction, not
# a design: nine of its entries were CLI-less primitives that
# ``test_agent_facing_partition`` nonetheless forced to ``agent_facing=True``
# (``verb=validate`` implied the flag unconditionally), so this gate failed on
# them and the exemption set was the patch. That contradiction is now resolved
# at the source — a primitive with no ``CliShape`` declares
# ``agent_facing=False``, because neither the CLI dispatcher nor the MCP
# server can reach it — so the loop below skips them on the ``agent_facing``
# check and needs no allowlist.
#
# The other two entries (``recommend-partition``, ``update-run-constraints``)
# had grown real CLI subcommands and were exempting a check they already
# passed. Nothing pinned the set against that drift, which is the standing
# argument against reintroducing it: an exemption list with no liveness test
# rots silently, and here it was also hiding the flag bug that produced it.
#
# If this gate fails, the fix is one of: register a subparser in the
# appropriate ``cli/<module>.py:register()``; add a deliberate alias to
# ``_PRIMITIVE_TO_CLI_VERB``; or declare ``agent_facing=False`` if the
# primitive genuinely is a composed internal with no agent surface.


def _live_subcommands() -> set[str]:
    parser = build_parser()
    import argparse

    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            # Include verb-group subcommands (clusters/campaign nested
            # subparsers). The parent verb is in the top-level choices;
            # the children appear only inside that subparser's tree.
            top = set(action.choices)
            nested: set[str] = set()
            for verb, sub_parser in action.choices.items():
                for nested_action in sub_parser._actions:
                    if isinstance(nested_action, argparse._SubParsersAction):
                        nested.update(f"{verb} {child}" for child in nested_action.choices)
            return top | nested
    return set()


@pytest.fixture(scope="module")
def cli_verbs() -> set[str]:
    return _live_subcommands()


def test_every_agent_facing_primitive_has_a_cli_subcommand(cli_verbs: set[str]) -> None:
    """Pin: agent-facing primitives are reachable via the CLI."""
    missing: list[str] = []
    # Filter to core-only: a plugin's primitives are wired into the CLI by
    # the plugin itself (its own cli_register entry point); the core CLI parser
    # this test inspects intentionally only sees core verbs.
    for entry in core_only_operations_catalog():
        if not entry.get("agent_facing"):
            continue
        name = entry["name"]
        verb = _PRIMITIVE_TO_CLI_VERB.get(name, name)
        # Verb-group children are space-separated in cli_verbs ("clusters list").
        if verb in cli_verbs:
            continue
        if any(v.endswith(f" {name}") for v in cli_verbs):
            continue
        missing.append(name)

    if missing:
        raise AssertionError(
            "The following agent-facing primitives have no CLI subcommand:\n"
            + "\n".join(f"  - {n}" for n in sorted(missing))
            + "\n\nEither register a subparser in the appropriate "
            "cli/<module>.py:register() function, add an entry to "
            "_PRIMITIVE_TO_CLI_VERB if it's intentionally aliased, or "
            "declare agent_facing=False if it is a composed internal with "
            "no agent surface (an agent cannot invoke what has no CLI "
            "subcommand and no MCP tool). Do not reintroduce a blanket "
            "exemption set — see the note above."
        )


def test_no_dead_aliases_in_primitive_to_cli_verb() -> None:
    """Pin: every entry in _PRIMITIVE_TO_CLI_VERB names a real primitive."""
    known = {e["name"] for e in core_only_operations_catalog()}
    dead = sorted(set(_PRIMITIVE_TO_CLI_VERB) - known)
    assert not dead, (
        "_PRIMITIVE_TO_CLI_VERB entries name primitives that no longer exist: "
        f"{dead}. Remove the stale entries."
    )
