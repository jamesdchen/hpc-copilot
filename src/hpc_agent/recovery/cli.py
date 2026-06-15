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

from hpc_agent import errors
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
            CliArg(
                "--placeholders",
                type=str,
                default=None,
                help=(
                    "Comma-separated ``key=value`` pairs to substitute "
                    "into ``<token>`` placeholders in recovery option "
                    "commands. Example: ``--placeholders "
                    "run_id=foo-bar,scheduler=sge,experiment_dir=/abs/path``. "
                    "Unsubstituted tokens pass through verbatim so a "
                    "downstream renderer can still substitute. Resolves "
                    "WS3 design questions 2 (per-cluster variants via "
                    "``<scheduler>``) and 5 (placeholder substitution "
                    "lives on the CLI side, not the caller side)."
                ),
            ),
        ),
        group="recoveries",
        verb="show",
    ),
    agent_facing=True,
)
def recoveries_show(*, kind: str, placeholders: str | None = None) -> dict[str, Any]:
    """Return the :class:`RecoveryMenu` for *kind* as a JSON-able dict.

    *placeholders* is a comma-separated ``key=value`` string substituted
    into each option's ``cli_command`` and into the
    ``rendered_remediation`` field. Malformed pieces (no ``=``, empty
    key) are silently skipped — substitution is best-effort and
    unsubstituted tokens pass through unchanged.

    Raises ``errors.SpecInvalid`` (surfaces as ``spec_invalid`` / exit 1
    via the dispatch layer, which only maps ``HpcError`` subclasses) when
    *kind* is not ported. A bare ``KeyError`` would fall through to the
    last-resort handler and mislabel as ``internal`` / exit 3. The error
    message names the available kinds so a caller can recover.
    """
    if kind not in REGISTRY:
        known = sorted(PORTED_KINDS)
        raise errors.SpecInvalid(
            f"recoveries show: kind={kind!r} not in registry; ported kinds are {known}"
        )
    subs = _parse_placeholders(placeholders)
    menu = menu_for(kind)
    # Substitute placeholders into per-option cli_command too so the
    # JSON consumer doesn't have to re-parse the rendered remediation
    # to extract the substituted command shapes.
    import re as _re

    _pat = _re.compile(r"<([A-Za-z_][A-Za-z0-9_]*)>")

    def _apply(cmd: str) -> str:
        return _pat.sub(lambda m: subs.get(m.group(1), m.group(0)), cmd)

    return {
        "kind": menu.kind,
        "summary": menu.summary,
        "options": [
            {
                "cli_command": _apply(opt.cli_command),
                "when_to_use": opt.when_to_use,
                "safety_rank": opt.safety_rank,
            }
            for opt in sorted(menu.options, key=lambda o: o.safety_rank)
        ],
        "references": list(menu.references or ()),
        "rendered_remediation": menu.remediation_text(placeholders=subs),
        "placeholders": subs,
    }


def _parse_placeholders(raw: str | None) -> dict[str, str]:
    """Parse ``--placeholders k1=v1,k2=v2`` into a dict.

    Best-effort: empty pieces and pieces without ``=`` are silently
    skipped (the caller may have passed a trailing comma or a stray
    space). Keys and values are stripped of surrounding whitespace.
    """
    if not raw:
        return {}
    out: dict[str, str] = {}
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece or "=" not in piece:
            continue
        k, _, v = piece.partition("=")
        k = k.strip()
        v = v.strip()
        if k:
            out[k] = v
    return out
