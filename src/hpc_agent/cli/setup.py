"""Setup-domain primitives — install-commands, setup, describe, find.

Each verb is wired as an ``@primitive`` whose decorator carries a
:class:`CliShape` with a ``handler=`` escape hatch. The handlers do
the same hand-written CLI work the prior Tier-3 adapters did (env
exit codes, branching, etc.); the @primitive decoration surfaces the
verb in the operations catalog and the registry-driven parser walk so
they're indistinguishable from any other primitive from the agent's
point of view.

Capabilities lives in :mod:`hpc_agent._kernel.extension.capabilities`
alongside its envelope builder.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from hpc_agent._kernel.registry.plugins import run_plugin_setup_actions
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent.cli._dispatch import CliArg, CliShape
from hpc_agent.cli._helpers import EXIT_OK, _emit, _err, _ok


def _install_commands_handler(args: argparse.Namespace) -> int:
    return _emit_install_commands(args)


def _emit_install_commands(args: argparse.Namespace) -> int:
    from hpc_agent.agent_assets import install_agent_assets

    claude_dir = Path(args.claude_dir).expanduser() if args.claude_dir else None
    summary = install_agent_assets(claude_dir=claude_dir, dry_run=args.dry_run)
    _emit({"ok": True, "idempotent": True, "data": summary})
    return EXIT_OK


@primitive(
    name="install-commands",
    verb="scaffold",
    side_effects=[SideEffect("filesystem", "~/.claude/")],
    idempotent=True,
    idempotency_key="claude_dir",
    cli=CliShape(
        help=(
            "Copy the bundled slash commands and skills into "
            "~/.claude/commands/ and ~/.claude/skills/. The pip-install "
            "entry point — run once after `pip install hpc-agent` to wire "
            "the agent assets into Claude Code. Idempotent (overwrites in "
            "place). Pass --claude-dir to target a non-default config dir."
        ),
        args=(
            CliArg(
                "--dry-run",
                action="store_true",
                help="Preview which commands/skills would be copied without writing.",
            ),
            CliArg(
                "--claude-dir",
                type=str,
                default=None,
                help="Override the target Claude config dir. Defaults to ~/.claude.",
            ),
        ),
        handler=_install_commands_handler,
    ),
    agent_facing=True,
)
def install_commands(
    *, dry_run: bool = False, claude_dir: str | Path | None = None
) -> dict[str, Any]:
    """Copy bundled slash commands + skills into ``~/.claude/``.

    The pip-install entry point: after ``pip install hpc-agent`` this
    wires the agent assets shipped in the wheel into Claude Code's
    user-global config dir. Idempotent (overwrites in place). Pass
    ``dry_run=True`` to preview without writing.
    """
    from hpc_agent.agent_assets import install_agent_assets

    target = Path(claude_dir).expanduser() if claude_dir else None
    return install_agent_assets(claude_dir=target, dry_run=dry_run)


def _setup_handler(args: argparse.Namespace) -> int:
    from hpc_agent.agent_assets import install_agent_assets
    from hpc_agent.ops.preflight.check import check_preflight, write_preflight_marker

    claude_dir = Path(args.claude_dir).expanduser() if args.claude_dir else None
    assets = install_agent_assets(claude_dir=claude_dir, dry_run=args.dry_run)
    payload: dict[str, Any] = {"assets": assets}

    cluster = getattr(args, "cluster", None)
    experiment_dir = Path(args.experiment_dir).expanduser() if args.experiment_dir else Path.cwd()
    if cluster:
        preflight = check_preflight(cluster=cluster)
        payload["preflight"] = preflight
        if preflight["all_ok"] and not args.dry_run:
            marker = write_preflight_marker(cluster=cluster, experiment_dir=experiment_dir)
            payload["preflight_marker"] = str(marker)

    plugin_actions = run_plugin_setup_actions(
        {
            "cluster": cluster,
            "experiment_dir": str(experiment_dir),
            "install": bool(getattr(args, "install_cron", False)),
            "dry_run": args.dry_run,
        }
    )
    if plugin_actions:
        payload["plugin_actions"] = plugin_actions

    _emit({"ok": True, "idempotent": True, "data": payload})
    return EXIT_OK


@primitive(
    name="setup",
    verb="scaffold",
    composes=["install-commands", "check-preflight"],
    side_effects=[
        SideEffect("filesystem", "~/.claude/"),
        SideEffect("ssh", "<cluster>"),
    ],
    idempotent=True,
    idempotency_key="cluster",
    cli=CliShape(
        help=(
            "One-shot setup: copy the bundled slash commands and skills "
            "into ~/.claude/. Run this once after `pip install "
            "hpc-agent`. Idempotent — safe to re-run."
        ),
        args=(
            CliArg(
                "--dry-run",
                action="store_true",
                help="Preview without writing.",
            ),
            CliArg(
                "--claude-dir",
                type=str,
                default=None,
                help="Override the target Claude config dir. Defaults to ~/.claude.",
            ),
            CliArg(
                "--cluster",
                type=str,
                default=None,
                help=(
                    "Optional cluster name. When supplied, probe the cluster's "
                    "environment (SSH agent, ssh/transport on PATH, clusters.yaml, "
                    "TCP :22) after install and write the 24h cache marker that "
                    "/submit-hpc's Step 6b gate reads."
                ),
            ),
            CliArg(
                "--experiment-dir",
                type=str,
                default=None,
                help=(
                    "Experiment directory whose journal receives the preflight "
                    "cache marker. Defaults to cwd. Only used when --cluster is set."
                ),
            ),
            CliArg(
                "--install-cron",
                action="store_true",
                help=(
                    "Opt in to any setup-time install actions an installed "
                    "plugin offers (passed to plugins as install=True). With no "
                    "such plugin loaded this is a no-op. The plugin decides what "
                    "the action is and what it requires; results are reported "
                    "under the envelope's `plugin_actions` field."
                ),
            ),
        ),
        handler=_setup_handler,
    ),
    agent_facing=True,
)
def setup(
    *,
    dry_run: bool = False,
    claude_dir: str | Path | None = None,
    cluster: str | None = None,
    experiment_dir: str | Path | None = None,
    install_cron: bool = False,
) -> dict[str, Any]:
    """One-shot setup: install bundled assets; optionally probe a cluster.

    Installs the bundled slash commands + skills into ``~/.claude/``.
    With *cluster* supplied, also probes the cluster's environment
    (SSH agent, ssh + file-transfer transport on PATH, ``clusters.yaml``
    parses, TCP :22 reachable) and — on a green probe — writes the 24h
    cache marker that ``/submit-hpc``'s Step 6b gate reads.

    Any installed plugin's optional setup hook is invoked with a context
    dict (``cluster``, ``experiment_dir``, ``install``, ``dry_run``); the
    *install_cron* flag sets ``install=True``. Each plugin's returned
    contribution is collected under the envelope's ``plugin_actions``
    field, keyed by plugin name. On a core-only install no plugin
    contributes and the field is absent. The host names no specific
    plugin action — what an action does (e.g. installing a snapshot
    cron) is entirely the plugin's concern.

    The marker is scoped to *experiment_dir* (default: cwd) because
    the Step 6b gate reads from ``JournalLayout(experiment_dir)`` — run
    setup from your experiment directory or pass an explicit
    *experiment_dir*.
    """
    from hpc_agent.agent_assets import install_agent_assets
    from hpc_agent.ops.preflight.check import check_preflight, write_preflight_marker

    target_claude_dir = Path(claude_dir).expanduser() if claude_dir else None
    assets = install_agent_assets(claude_dir=target_claude_dir, dry_run=dry_run)
    payload: dict[str, Any] = {"assets": assets}

    exp = Path(experiment_dir).expanduser() if experiment_dir else Path.cwd()
    if cluster:
        preflight = check_preflight(cluster=cluster)
        payload["preflight"] = preflight
        if preflight["all_ok"] and not dry_run:
            marker = write_preflight_marker(cluster=cluster, experiment_dir=exp)
            payload["preflight_marker"] = str(marker)

    plugin_actions = run_plugin_setup_actions(
        {
            "cluster": cluster,
            "experiment_dir": str(exp),
            "install": install_cron,
            "dry_run": dry_run,
        }
    )
    if plugin_actions:
        payload["plugin_actions"] = plugin_actions

    return payload


def _describe_handler(args: argparse.Namespace) -> int:
    # Move 2 (proving-run-2-hardening §3): `--schema` routes an agent to the
    # RESOLVED input-schema content instead of the bare filename `describe`
    # otherwise returns — so a caller blocked from `cat`/`python -c` never has
    # to `find /` a schema file. Additive: no change to the `describe()`
    # primitive signature or its registry row (the flag lives on the argparse
    # subparser, not in the CliShape.args baked into operations.json).
    if getattr(args, "schema", False):
        return _emit_describe_schema(args.name)
    return _emit_describe(args.name)


def _valid_ref_name(name: str) -> bool:
    """True when *name* is a lowercase procedure/skill/primitive identifier."""
    return bool(
        name and name[0].isalpha() and all(c.islower() or c.isdigit() or c == "-" for c in name)
    )


def _emit_describe_schema(name: str) -> int:
    """Emit a verb's RESOLVED input-schema JSON content (Move 2).

    Reuses the same package-data resolution as the MCP surface
    (:func:`schema_for` + ``_load_input_schema``) so a CLI caller gets the
    schema *content*, not its filename. The filesystem path is not an API;
    the package owns its schemas via ``importlib.resources``.
    """
    if not _valid_ref_name(name):
        return _err(
            error_code="spec_invalid",
            message=(
                f"name {name!r} must be lowercase letters, digits, and hyphens — a primitive name"
            ),
            category="user",
            retry_safe=False,
        )

    from hpc_agent._kernel.extension.mcp_server import _load_input_schema
    from hpc_agent._kernel.registry.operations import schema_for
    from hpc_agent._kernel.registry.primitive import get_meta
    from hpc_agent.cli._dispatch import cli_to_invocation_string

    try:
        meta = get_meta(name)
    except (KeyError, RuntimeError):
        return _err(
            error_code="spec_invalid",
            message=f"no primitive named {name!r}",
            category="user",
            retry_safe=False,
        )

    backed = {
        "python": f"{meta.func.__module__}.{meta.func.__qualname__}",
        "cli": cli_to_invocation_string(meta.name, meta.cli),
    }
    fname = schema_for(name, "input", backed)
    if not fname:
        return _err(
            error_code="spec_invalid",
            message=f"primitive {name!r} declares no input schema",
            category="user",
            retry_safe=False,
        )
    # schema_for returns the packaged filename (e.g. "append_decision.input.json");
    # _load_input_schema wants the basename it appends ".input.json" to.
    basename = fname[: -len(".input.json")]
    schema = _load_input_schema(basename)
    if schema is None:
        return _err(
            error_code="spec_invalid",
            message=f"input schema for {name!r} could not be loaded",
            category="user",
            retry_safe=False,
        )
    _ok({"kind": "input_schema", "name": name, "schema": schema})
    return EXIT_OK


def _emit_describe(name: str) -> int:
    if not (
        name and name[0].isalpha() and all(c.islower() or c.isdigit() or c == "-" for c in name)
    ):
        return _err(
            error_code="spec_invalid",
            message=(
                f"name {name!r} must be lowercase letters, digits, and "
                "hyphens — a procedure, skill, or primitive name"
            ),
            category="user",
            retry_safe=False,
        )

    # #261: describe output is framework-stable (changes only with the package
    # version), so memoize the resolved data payload by (pkg_version, name). A
    # hit skips the registry load + procedure/skill resolution entirely. Only
    # successful describes are cached; a not-found name re-runs the live path.
    from hpc_agent.state import describe_cache

    cached = describe_cache.load(name)
    if cached is not None:
        _ok(cached)
        return EXIT_OK

    try:
        data = describe(name=name)
    except ValueError:
        return _err(
            error_code="spec_invalid",
            message=f"no skill or primitive named {name!r}",
            category="user",
            retry_safe=False,
        )

    describe_cache.store(name, data)
    _ok(data)
    return EXIT_OK


@primitive(
    name="describe",
    verb="query",
    side_effects=[],
    idempotent=True,
    cli=CliShape(
        help=(
            "Print a skill's procedure or a primitive's contract from the "
            "installed package data. A delegated worker uses this to fetch "
            "a cross-reference on the branch it is executing."
        ),
        args=(
            CliArg(
                "name",
                help="A skill name (e.g. hpc-submit) or a primitive name (e.g. submit-flow).",
            ),
        ),
        handler=_describe_handler,
    ),
    agent_facing=True,
)
def describe(*, name: str) -> dict[str, Any]:
    """Resolve *name* to its content from package data.

    Resolution order:

    1. Inline skill (``slash_commands/skills/<name>/SKILL.md``) —
       returns ``kind: "skill"``.
    2. Primitive in the operations catalog — returns ``kind:
       "primitive"`` with its contract.

    (Worker-prompt procedures — ``kind: "procedure"`` — were the bare-worker
    spawn transport's surface; deleted with it in the §6 worker removal. The
    block-drive skills are the workflow entry points now.)
    """
    from importlib.resources import files

    skill_md = files("slash_commands") / "skills" / name / "SKILL.md"
    if skill_md.is_file():
        body = skill_md.read_text(encoding="utf-8")
        if body.startswith("---"):
            close = body.find("\n---", 3)
            if close != -1:
                body = body[close + 4 :]
        return {"kind": "skill", "name": name, "content": body.strip()}

    from hpc_agent._kernel.registry.operations import operations_catalog

    for entry in operations_catalog():
        if entry.get("name") == name:
            return {"kind": "primitive", "name": name, "content": entry}

    raise ValueError(f"no skill or primitive named {name!r}")


def _find_handler(args: argparse.Namespace) -> int:
    _ok(find(query=args.query, limit=args.limit), name="find")
    return EXIT_OK


@primitive(
    name="find",
    verb="query",
    side_effects=[],
    idempotent=True,
    cli=CliShape(
        help=(
            "Search the operations catalog by intent or half-remembered "
            "name and return a thin candidate list ({name, verb, cli, "
            "summary}) — the explore step between the full-catalog dump "
            "(`capabilities --full`) and a single contract (`describe "
            "<name>`). Pass a phrase like 'submit a batch'; cap with --limit."
        ),
        args=(
            CliArg(
                "query",
                help="Intent phrase or partial primitive name, e.g. 'submit a batch'.",
            ),
            CliArg(
                "--limit",
                type=int,
                default=15,
                help="Maximum candidates to return (default 15).",
            ),
        ),
        handler=_find_handler,
    ),
    agent_facing=True,
)
def find(*, query: str, limit: int = 15) -> dict[str, Any]:
    """Search the operations catalog → a thin candidate list.

    The middle discovery tier between dumping the whole catalog
    (``capabilities --full``) and fetching one contract
    (``describe <name>``). Returns only ``{name, verb, cli, summary}``
    per match — no schemas, no doc bodies — so an agent resolves intent
    to a short list of names cheaply, then ``describe``s the one it wants.

    Matching is stdlib-only (no index, no embeddings): a fuzzy
    :func:`difflib.get_close_matches` pass over primitive *names* (for a
    half-remembered name like ``submit-batch`` → ``submit-flow-batch``)
    unioned with a token / substring scan over ``name + summary`` (for an
    intent phrase like ``submit a batch``). The union is returned in
    stable catalog order, capped at *limit*. A blank query matches
    nothing rather than dumping the catalog — that is what
    ``capabilities`` is for.
    """
    import difflib
    import re

    from hpc_agent._kernel.registry.operations import operations_catalog

    catalog = operations_catalog()
    needle = query.strip().lower()
    # Clamp negatives to 0 so a stray ``--limit -1`` returns nothing rather
    # than silently lopping the last row off via ``rows[:-1]``.
    limit = max(limit, 0)
    if not needle or limit == 0:
        return {"query": query, "count": 0, "matches": []}

    names = [entry["name"] for entry in catalog]
    fuzzy = set(difflib.get_close_matches(needle, names, n=limit, cutoff=0.5))

    tokens = [t for t in re.split(r"\W+", needle) if t]
    keyword: set[str] = set()
    for entry in catalog:
        haystack = f"{entry['name']} {entry.get('summary') or ''}".lower()
        if needle in haystack or (tokens and all(t in haystack for t in tokens)):
            keyword.add(entry["name"])

    matched = fuzzy | keyword
    rows = [
        {
            "name": entry["name"],
            "verb": entry["verb"],
            "cli": entry.get("cli"),
            "summary": entry.get("summary") or "",
        }
        for entry in catalog
        if entry["name"] in matched
    ][:limit]
    return {"query": query, "count": len(rows), "matches": rows}


# Back-compat alias: external callers (tests, slash commands) still use
# the ``cmd_*`` names from when these were Tier-3 hand-written adapters.
# These re-bind to the new handlers so behaviour is identical.
cmd_install_commands = _install_commands_handler
cmd_setup = _setup_handler
cmd_describe = _describe_handler
cmd_find = _find_handler


def cmd_capabilities(args: argparse.Namespace) -> int:
    """Back-compat alias: delegates to the canonical handler."""
    from hpc_agent._kernel.extension.capabilities import _capabilities_handler

    return _capabilities_handler(args)


def register(sub: argparse._SubParsersAction) -> None:
    """Back-compat no-op: the four verbs are now ``@primitive`` entries.

    Kept so external callers that still import ``setup.register`` keep
    working; the registry-walking parser at
    :func:`hpc_agent.cli.parser._register_from_registry` picks up the
    four verbs from their decorators. New callers should not need this.
    """
    return None


__all__ = [
    "cmd_capabilities",
    "cmd_describe",
    "cmd_find",
    "cmd_install_commands",
    "cmd_setup",
    "describe",
    "find",
    "install_commands",
    "register",
    "setup",
]
