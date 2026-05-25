"""Setup-domain primitives — install-commands, setup, describe.

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
    if cluster:
        preflight = check_preflight(cluster=cluster)
        payload["preflight"] = preflight
        if preflight["all_ok"] and not args.dry_run:
            experiment_dir = (
                Path(args.experiment_dir).expanduser() if args.experiment_dir else Path.cwd()
            )
            marker = write_preflight_marker(cluster=cluster, experiment_dir=experiment_dir)
            payload["preflight_marker"] = str(marker)

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
) -> dict[str, Any]:
    """One-shot setup: install bundled assets; optionally probe a cluster.

    Installs the bundled slash commands + skills into ``~/.claude/``.
    With *cluster* supplied, also probes the cluster's environment
    (SSH agent, ssh + file-transfer transport on PATH, ``clusters.yaml``
    parses, TCP :22 reachable) and — on a green probe — writes the 24h
    cache marker that ``/submit-hpc``'s Step 6b gate reads, so the
    first submit in this experiment doesn't repeat the probe.

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

    if cluster:
        preflight = check_preflight(cluster=cluster)
        payload["preflight"] = preflight
        if preflight["all_ok"] and not dry_run:
            exp = Path(experiment_dir).expanduser() if experiment_dir else Path.cwd()
            marker = write_preflight_marker(cluster=cluster, experiment_dir=exp)
            payload["preflight_marker"] = str(marker)

    return payload


def _describe_handler(args: argparse.Namespace) -> int:
    return _emit_describe(args.name)


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

    from importlib.resources import files

    from hpc_agent._wire.spawn_contract import WORKFLOW_PROCEDURES

    if name in WORKFLOW_PROCEDURES:
        from hpc_agent._kernel.extension.spawn_prompt import _procedure_body

        _ok({"kind": "procedure", "name": name, "content": _procedure_body(name)})
        return EXIT_OK

    skill_md = files("slash_commands") / "skills" / name / "SKILL.md"
    if skill_md.is_file():
        body = skill_md.read_text(encoding="utf-8")
        if body.startswith("---"):
            close = body.find("\n---", 3)
            if close != -1:
                body = body[close + 4 :]
        _ok({"kind": "skill", "name": name, "content": body.strip()})
        return EXIT_OK

    from hpc_agent._kernel.registry.operations import operations_catalog

    for entry in operations_catalog():
        if entry.get("name") == name:
            _ok({"kind": "primitive", "name": name, "content": entry})
            return EXIT_OK

    return _err(
        error_code="spec_invalid",
        message=f"no skill or primitive named {name!r}",
        category="user",
        retry_safe=False,
    )


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

    1. Worker-prompt procedure (``hpc_agent/_kernel/extension/worker_prompts/<name>.md``,
       with plugin overlay) — returns ``kind: "procedure"``.
    2. Inline skill (``slash_commands/skills/<name>/SKILL.md``) —
       returns ``kind: "skill"``.
    3. Primitive in the operations catalog — returns ``kind:
       "primitive"`` with its contract.
    """
    from importlib.resources import files

    from hpc_agent._wire.spawn_contract import WORKFLOW_PROCEDURES

    if name in WORKFLOW_PROCEDURES:
        from hpc_agent._kernel.extension.spawn_prompt import _procedure_body

        return {"kind": "procedure", "name": name, "content": _procedure_body(name)}

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


# Back-compat alias: external callers (tests, slash commands) still use
# the ``cmd_*`` names from when these were Tier-3 hand-written adapters.
# These re-bind to the new handlers so behaviour is identical.
cmd_install_commands = _install_commands_handler
cmd_setup = _setup_handler
cmd_describe = _describe_handler


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
    "cmd_install_commands",
    "cmd_setup",
    "describe",
    "install_commands",
    "register",
    "setup",
]
