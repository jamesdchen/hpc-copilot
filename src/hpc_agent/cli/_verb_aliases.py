"""Registry-name ⇄ CLI-verb aliasing — ONE definition, derived from the map.

The single-verb fast-path table (:data:`hpc_agent.cli._verb_module_map.VERB_MODULE_MAP`)
already records, per CLI verb, the ``(primitive_name, module)`` it dispatches.
Where a verb's CLI name differs from the primitive's *registry* name — the
canonical case being CLI ``reconcile`` → primitive ``reconcile-journal`` — this
module inverts that relationship so both directions are resolvable from the
same source. It hand-authors NO second table: everything is a projection of
``VERB_MODULE_MAP`` (run-#12 finding 22's "one alias map, shared" clause).

Consumers:

* ``cli/setup.py`` — ``describe`` resolves EITHER name and prints the CLI verb.
* ``cli/parser.py`` — the unknown-command path names the exact CLI verb to run.
* ``tests/contracts/test_guidance_uses_cli_verbs.py`` — the guidance lint reads
  the same differing-name set, so the lint and the resolvers can never drift.

Scope note: ``VERB_MODULE_MAP`` covers only ungrouped, handler-less verbs (the
fast-path's remit), so this map does NOT include handler-based Tier-2 aliases
(``submit-spec`` → ``submit``) or verb-group children (``campaign-init`` →
``campaign init``). It covers exactly the aliases the fast path — and finding
22's live failure — care about; ``reconcile-journal`` is one of them.
"""

from __future__ import annotations

from functools import lru_cache

from hpc_agent.cli._verb_module_map import VERB_MODULE_MAP


@lru_cache(maxsize=1)
def registry_name_to_cli_verb() -> dict[str, str]:
    """Map each primitive registry name to its CLI verb, where the two differ."""
    return {
        primitive_name: cli_verb
        for cli_verb, (primitive_name, _module) in VERB_MODULE_MAP.items()
        if primitive_name != cli_verb
    }


@lru_cache(maxsize=1)
def cli_verb_to_registry_name() -> dict[str, str]:
    """Map each CLI verb to the primitive registry name it dispatches."""
    return {verb: primitive for verb, (primitive, _module) in VERB_MODULE_MAP.items()}


def cli_verb_for_registry_name(name: str) -> str | None:
    """Return *name*'s CLI verb when it is a registry name with a differing verb.

    ``None`` when *name* is already a CLI verb, an identity-named primitive, or
    not covered by the fast-path map at all — i.e. nothing needs re-pointing.
    """
    return registry_name_to_cli_verb().get(name)


def resolve_to_registry_name(name: str) -> str:
    """Return the registry name *name* addresses — remapping a CLI verb if so.

    A CLI verb whose primitive is named differently (``reconcile`` →
    ``reconcile-journal``) resolves to the registry name; every other input —
    a registry name, an identity verb, a skill name — is returned unchanged.
    """
    return cli_verb_to_registry_name().get(name, name)


def display_verb_for(name: str) -> str:
    """Return the CLI verb to SHOW for *name* (a registry name or a CLI verb).

    Resolves *name* to its registry name, then to that primitive's CLI verb, so
    ``describe reconcile-journal`` and ``describe reconcile`` both print
    ``reconcile`` and agent-facing guidance stays in CLI-verb terms.
    """
    registry_name = resolve_to_registry_name(name)
    return registry_name_to_cli_verb().get(registry_name, registry_name)


def differing_registry_names() -> frozenset[str]:
    """The set of registry names whose CLI verb differs — the lint's needle set."""
    return frozenset(registry_name_to_cli_verb())


__all__ = [
    "cli_verb_for_registry_name",
    "cli_verb_to_registry_name",
    "differing_registry_names",
    "display_verb_for",
    "registry_name_to_cli_verb",
    "resolve_to_registry_name",
]
