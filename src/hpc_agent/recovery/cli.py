"""CLI verbs for the recovery registry.

Exposes ``hpc-agent recoveries list`` and ``hpc-agent recoveries show
--kind <name>`` so SKILL.md prose can reference enumerated options
without embedding literal text.

Wired as ``@primitive`` so the registry walk picks the verbs up — the
``recoveries`` parent verb is the group, with ``list`` / ``show`` as
its leaves (mirroring the ``clusters`` and ``campaign`` verb groups).
"""

from __future__ import annotations

from typing import Any

from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent.cli._dispatch import CliArg, CliShape
from hpc_agent.recovery.registry import (
    PORTED_KINDS,
    REGISTRY,
    all_kinds,
    menu_for,
)


@primitive(
    name="recoveries-list",
    verb="query",
    side_effects=[],
    idempotent=True,
    cli=CliShape(
        help=(
            "List every failure ``kind`` known to the recovery registry. "
            "Reports both ported kinds (with menus) and the un-ported "
            "punch list, so a SKILL.md author can see what recoveries are "
            "available before referencing one."
        ),
        group="recoveries",
        verb="list",
    ),
    agent_facing=True,
)
def recoveries_list() -> dict[str, Any]:
    """Return the catalog of registry kinds."""
    known = all_kinds()
    return {
        "ported_kinds": sorted(PORTED_KINDS),
        "unported_kinds": sorted(set(known) - PORTED_KINDS),
        "n_ported": len(PORTED_KINDS),
        "n_total": len(known),
    }


@primitive(
    name="recoveries-show",
    verb="query",
    side_effects=[],
    idempotent=True,
    cli=CliShape(
        help=(
            "Print the canonical recovery menu for one failure kind. "
            "Use this from SKILL.md prose to reference the menu by kind "
            "name instead of re-embedding the literal options."
        ),
        args=(
            CliArg(
                "--kind",
                type=str,
                required=True,
                help="The failure kind (one of ``recoveries list``).",
            ),
        ),
        group="recoveries",
        verb="show",
    ),
    agent_facing=True,
)
def recoveries_show(*, kind: str) -> dict[str, Any]:
    """Return the :class:`RecoveryMenu` for *kind* as a JSON-able dict.

    Raises ``KeyError`` (surfaces as ``spec_invalid`` via the dispatch
    layer) when *kind* is not ported. The error message names the
    available kinds so a caller can recover.
    """
    if kind not in REGISTRY:
        known = sorted(PORTED_KINDS)
        raise KeyError(f"recoveries show: kind={kind!r} not in registry; ported kinds are {known}")
    menu = menu_for(kind)
    return {
        "kind": menu.kind,
        "summary": menu.summary,
        "options": [
            {
                "cli_command": opt.cli_command,
                "when_to_use": opt.when_to_use,
                "safety_rank": opt.safety_rank,
            }
            for opt in sorted(menu.options, key=lambda o: o.safety_rank)
        ],
        "references": list(menu.references or ()),
        "rendered_remediation": menu.remediation_text(),
    }
