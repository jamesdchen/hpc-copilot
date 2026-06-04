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
    "discover-executors": "discover",
    # Verb-group children — space-separated form is what `_live_subcommands`
    # surfaces for nested subparsers (``hpc-agent campaign init`` etc.).
    "campaign-init": "campaign init",
    "campaign-list": "campaign list",
    "campaign-status": "campaign status",
    "campaign-replay": "campaign replay",
    "campaign-converged": "campaign converged",
    "campaign-budget": "campaign budget",
    "campaign-advance": "campaign advance",
    "campaign-health": "campaign health",
    "clusters-list": "clusters list",
    "clusters-describe": "clusters describe",
    "recoveries-list": "recoveries list",
    "recoveries-show": "recoveries show",
}

# Primitives that intentionally have no standalone CLI subcommand —
# composed into other primitives or workflows. Each entry justifies
# the exception so a future maintainer adding/removing one has to
# argue for it explicitly.
_INTENTIONALLY_NO_CLI: set[str] = {
    # Validators that are part of the ``validate-campaign`` family but
    # have no standalone CLI verb. Five of them
    # (validate-executor-signatures, validate-input-dataset,
    # validate-stochastic-marker, validate-walltime-against-history,
    # dry-run-local) are explicitly composed into ``validate-campaign``;
    # ``validate-self-qos-limit`` is registered for schema/contract
    # symmetry but not yet wired into the workflow body.
    "dry-run-local",
    "validate-executor-signatures",
    "validate-input-dataset",
    "validate-self-qos-limit",
    "validate-stochastic-marker",
    "validate-walltime-against-history",
    # Helpers composed by ``submit-flow``'s batch path; the agent
    # surface for the same effect is just re-running ``submit-flow``.
    "prune-orphan-sidecars",
    # Composed by ``plan-throughput`` and ``submit-flow``; no
    # independent agent use case.
    "recommend-partition",
    # Sidecar-mutating helper called from ``resubmit-failed``'s
    # constraint-override path; not part of the agent surface.
    "update-run-constraints",
}


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
        if name in _INTENTIONALLY_NO_CLI:
            continue
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
            "_PRIMITIVE_TO_CLI_VERB if it's intentionally aliased, "
            "or add it to _INTENTIONALLY_NO_CLI with a comment "
            "justifying the exception."
        )


def test_no_dead_aliases_in_primitive_to_cli_verb() -> None:
    """Pin: every entry in _PRIMITIVE_TO_CLI_VERB names a real primitive."""
    known = {e["name"] for e in core_only_operations_catalog()}
    dead = sorted(set(_PRIMITIVE_TO_CLI_VERB) - known)
    assert not dead, (
        "_PRIMITIVE_TO_CLI_VERB entries name primitives that no longer exist: "
        f"{dead}. Remove the stale entries."
    )
