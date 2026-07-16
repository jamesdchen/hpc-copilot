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
        # Fast-path safe (latency rank 13): the handler copies bundled assets
        # into ~/.claude — it never introspects the registry, so serving it on
        # the single-verb fast path is byte-identical, only faster. This verb
        # rides EVERY submit-preflight, so the ~9 s full-walk tax it paid was
        # per-submit dead weight.
        fast_path_safe=True,
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
    from hpc_agent.cli._helpers import _err_from_hpc
    from hpc_agent.ops.preflight.check import _preflight_exit_error, check_preflight

    claude_dir = Path(args.claude_dir).expanduser() if args.claude_dir else None
    assets = install_agent_assets(claude_dir=claude_dir, dry_run=args.dry_run)
    payload: dict[str, Any] = {"assets": assets}

    cluster = getattr(args, "cluster", None)
    experiment_dir = Path(args.experiment_dir).expanduser() if args.experiment_dir else Path.cwd()
    preflight_error = None
    if cluster:
        preflight = check_preflight(cluster=cluster)
        payload["preflight"] = preflight
        preflight_error = _preflight_exit_error(preflight)

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

    # A red cluster probe must exit non-zero + ok:false (#F31): before this,
    # ``setup --cluster`` emitted ``ok:true`` / ``EXIT_OK`` regardless of
    # ``preflight['all_ok']``, so a scripted bootstrap (or a glance at ``$?``)
    # proceeded believing setup succeeded over a broken environment. The assets
    # were still installed; the failure being reported is the cluster probe, with
    # the failing checks carried in the error envelope's ``failure_features``.
    if preflight_error is not None:
        return _err_from_hpc(preflight_error)

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
                    "TCP :22) after install; a failing probe exits non-zero "
                    "(cluster-error) so a scripted bootstrap sees the failure."
                ),
            ),
            CliArg(
                "--experiment-dir",
                type=str,
                default=None,
                help=(
                    "Experiment directory passed to plugin setup hooks. Defaults "
                    "to cwd. Only used when --cluster is set."
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
    parses, TCP :22 reachable) and reports the verdict under ``preflight``.
    The CLI handler (:func:`_setup_handler`) exits cluster-error on a red
    probe; this in-process form returns the verdict in the payload for a
    caller to inspect.

    Any installed plugin's optional setup hook is invoked with a context
    dict (``cluster``, ``experiment_dir``, ``install``, ``dry_run``); the
    *install_cron* flag sets ``install=True``. Each plugin's returned
    contribution is collected under the envelope's ``plugin_actions``
    field, keyed by plugin name. On a core-only install no plugin
    contributes and the field is absent. The host names no specific
    plugin action — what an action does (e.g. installing a snapshot
    cron) is entirely the plugin's concern.
    """
    from hpc_agent.agent_assets import install_agent_assets
    from hpc_agent.ops.preflight.check import check_preflight

    target_claude_dir = Path(claude_dir).expanduser() if claude_dir else None
    assets = install_agent_assets(claude_dir=target_claude_dir, dry_run=dry_run)
    payload: dict[str, Any] = {"assets": assets}

    exp = Path(experiment_dir).expanduser() if experiment_dir else Path.cwd()
    if cluster:
        preflight = check_preflight(cluster=cluster)
        payload["preflight"] = preflight

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


def _resolve_catalog() -> list[dict[str, Any]]:
    """Return the operations catalog to answer ``describe`` / ``find`` against.

    On the full path (the registry has been fully walked) this is the live
    :func:`operations_catalog`. On the single-verb fast path the live registry is
    PARTIAL — only the one module the verb needed was imported — and answering a
    discovery verb off it would be wrong-but-plausible (premortem A1: a ``find``
    silently matches ~4 rows, a ``describe`` of any other verb confidently
    errors). So hydrate from the shipped ``operations.json`` bake instead — but
    only when it is TRUSTWORTHY (``baked_catalog_usable`` is content-keyed on the
    build fingerprint, so a stale source-checkout bake is never trusted). If the
    bake is not trustworthy or cannot be read, refuse to answer off a partial
    registry and complete the full walk. Either way the returned catalog is the
    WHOLE truth, so the emitted envelope is byte-identical to the full path.
    """
    from hpc_agent._kernel.registry import primitive as _prim
    from hpc_agent._kernel.registry.operations import operations_catalog

    if not getattr(_prim, "_REGISTRATION_DONE", False):
        if _prim.baked_catalog_usable():
            baked = _prim.load_baked_catalog()
            if baked is not None:
                return baked
        _prim.register_primitives()
    return operations_catalog()


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


def _did_you_mean(name: str, catalog: list[dict[str, Any]] | None = None) -> str:
    """A `` Did you mean: a, b?`` suffix of close primitive-name matches, else ``''``.

    Mirrors the CLI parser's unknown-command suggester (:class:`_HpcArgumentParser`)
    so a not-found ``describe`` names candidate primitives instead of dead-ending.
    *catalog* is threaded in by the fast path (a hydrated bake) so the suggestion
    set is the WHOLE surface there too — byte-identical to the full path — rather
    than the partial live registry.
    """
    import difflib

    if catalog is None:
        from hpc_agent._kernel.registry.operations import operations_catalog

        try:
            catalog = operations_catalog()
        except RuntimeError:
            # Registry not fully populated (partial fast-path / a monkeypatched
            # unit test) — a suggestion is a nicety, never worth crashing the
            # not-found envelope over.
            return ""
    names = [entry.get("name", "") for entry in catalog]
    close = difflib.get_close_matches(name, names, n=3, cutoff=0.5)
    return f" Did you mean: {', '.join(close)}?" if close else ""


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
    from hpc_agent.cli._verb_aliases import resolve_to_registry_name

    # Resolve EITHER name (run-#12 finding 22): a caller may pass the registry
    # name (`reconcile-journal`) OR the CLI verb (`reconcile`). Look the
    # primitive up by its registry name; a differing CLI verb remaps to it.
    registry_name = resolve_to_registry_name(name)

    try:
        meta = get_meta(registry_name)
    except (KeyError, RuntimeError):
        return _err(
            error_code="spec_invalid",
            message=f"no primitive named {name!r}.{_did_you_mean(registry_name)}",
            category="user",
            retry_safe=False,
        )

    invocation = cli_to_invocation_string(meta.name, meta.cli)
    backed = {
        "python": f"{meta.func.__module__}.{meta.func.__qualname__}",
        "cli": invocation,
    }
    fname = schema_for(registry_name, "input", backed)
    if not fname:
        # The primitive EXISTS but takes no --spec (its inputs are CLI flags):
        # say so, and name the CLI invocation so the caller does not burn a
        # round-trip discovering the verb (`reconcile-journal` → `reconcile`).
        return _err(
            error_code="spec_invalid",
            message=(
                f"primitive {registry_name!r} declares no input schema — it takes "
                f"no --spec. Invoke it directly: {invocation}"
            ),
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

    # Resolve EITHER name (run-#12 finding 22): look the primitive up by its
    # registry name, but emit the CLI verb as ``name`` so guidance that echoes a
    # describe stays in CLI-verb terms (`reconcile-journal` and `reconcile` both
    # describe cleanly and both print `reconcile`).
    from hpc_agent.cli._verb_aliases import display_verb_for, resolve_to_registry_name

    registry_name = resolve_to_registry_name(name)
    display_name = display_verb_for(name)

    # #261: describe output is framework-stable (changes only with the package
    # version), so memoize the resolved data payload by (pkg_version, name). A
    # hit skips the registry load + procedure/skill resolution entirely. Only
    # successful describes are cached; a not-found name re-runs the live path.
    from hpc_agent.state import describe_cache

    cached = describe_cache.load(display_name)
    if cached is not None:
        _ok(cached)
        return EXIT_OK

    # Fast path (partial live registry): resolve against the hydrated bake, never
    # the ~4-entry partial registry (premortem A1). ``_resolve_catalog`` returns
    # the whole-truth catalog either way, so the emitted bytes match the full
    # path. ``describe_cache.store`` still refuses under a partial registry, so a
    # fast-path-computed row is never persisted (it would poison full-path readers).
    catalog = _resolve_catalog()
    try:
        data = describe(name=registry_name, _catalog=catalog)
    except ValueError:
        return _err(
            error_code="spec_invalid",
            message=f"no skill or primitive named {name!r}.{_did_you_mean(registry_name, catalog)}",
            category="user",
            retry_safe=False,
        )

    # Surface the CLI verb as the canonical name so a caller that pasted a
    # registry name learns the invocable form.
    data = {**data, "name": display_name}
    describe_cache.store(display_name, data)
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
            # Move 2 (proving-run-2-hardening §3): `describe --schema` routes to the
            # RESOLVED input-schema CONTENT (not just the filename), so a caller
            # blocked from `cat`/`python -c` never has to `find /` a schema file.
            # Latency A2: it lives IN the CliShape.args so the operations.json bake
            # is the whole truth (no hand `add_argument` in the parser walk that the
            # bake can't see). The handler (`_describe_handler`) reads `args.schema`.
            CliArg(
                "--schema",
                action="store_true",
                help=(
                    "Emit the verb's resolved input-schema JSON content "
                    "(not just its filename), so callers never `find`/`cat` "
                    "a schema file."
                ),
            ),
        ),
        handler=_describe_handler,
        # Fast-path safe via BAKED HYDRATION (latency B4/B5): the handler reads
        # the operations catalog, which the single-verb fast path would leave
        # partial — so ``_emit_describe`` hydrates the WHOLE catalog from the
        # shipped ``operations.json`` bake (``_resolve_catalog``) before
        # answering, never the ~4-entry partial registry (premortem A1). Output
        # is byte-identical to the full walk; only the module-import tax is
        # saved. ``describe --schema`` is steered back to the full path by
        # ``_try_fast_dispatch`` (it resolves an ARBITRARY target verb's meta,
        # which the fast path has not imported).
        fast_path_safe=True,
    ),
    agent_facing=True,
)
def describe(*, name: str, _catalog: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Resolve *name* to its content from package data.

    Resolution order:

    1. Inline skill (``slash_commands/skills/<name>/SKILL.md``) —
       returns ``kind: "skill"``.
    2. Primitive in the operations catalog — returns ``kind:
       "primitive"`` with its contract.

    (Worker-prompt procedures — ``kind: "procedure"`` — were the bare-worker
    spawn transport's surface; deleted with it in the §6 worker removal. The
    block-drive skills are the workflow entry points now.)

    *_catalog* lets the CLI fast path inject a hydrated bake so a partial live
    registry is never consulted (premortem A1); when ``None`` the live
    :func:`operations_catalog` is used (the full path). The resolution is
    identical either way — only the catalog SOURCE differs — so the two paths
    return byte-identical rows.
    """
    from importlib.resources import files

    skill_md = files("hpc_agent.slash_commands") / "skills" / name / "SKILL.md"
    if skill_md.is_file():
        body = skill_md.read_text(encoding="utf-8")
        if body.startswith("---"):
            close = body.find("\n---", 3)
            if close != -1:
                body = body[close + 4 :]
        return {"kind": "skill", "name": name, "content": body.strip()}

    if _catalog is None:
        from hpc_agent._kernel.registry.operations import operations_catalog

        _catalog = operations_catalog()

    for entry in _catalog:
        if entry.get("name") == name:
            return {"kind": "primitive", "name": name, "content": entry}

    raise ValueError(f"no skill or primitive named {name!r}")


def _find_handler(args: argparse.Namespace) -> int:
    # Fast path resolves against the hydrated bake, never the partial live
    # registry (premortem A1: a partial registry silently matches ~4 rows). The
    # full path passes the same live catalog `find` would compute itself, so the
    # emitted bytes are identical.
    _ok(find(query=args.query, limit=args.limit, _catalog=_resolve_catalog()), name="find")
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
        # Fast-path safe via BAKED HYDRATION (latency B4/B5): ``_find_handler``
        # scans the WHOLE catalog, hydrated from the ``operations.json`` bake on
        # the fast path (``_resolve_catalog``) so a partial registry never
        # silently matches ~4 rows (premortem A1). Byte-identical to the full
        # walk; only the import tax is saved.
        fast_path_safe=True,
    ),
    agent_facing=True,
)
def find(
    *, query: str, limit: int = 15, _catalog: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
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

    if _catalog is None:
        from hpc_agent._kernel.registry.operations import operations_catalog

        _catalog = operations_catalog()

    catalog = _catalog
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
