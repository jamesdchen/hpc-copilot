"""Setup-domain CLI verbs — install-commands, setup, capabilities, describe.

These four verbs have no ``@primitive`` backing (they are CLI-only
orchestrators or wrappers whose adapter logic doesn't fit even with
:class:`hpc_agent.cli._dispatch.CliShape`'s rich hooks). Each lives
here as a hand-written ``cmd_*`` adapter plus a top-level
:func:`register` that wires the corresponding ``add_parser`` block
into the parent subparser.

* ``install-commands`` — wheel-asset installer (no primitive).
* ``setup`` — composes install-commands + check-preflight +
  write-preflight-marker; the side-effect orchestration doesn't fit
  spec_arg-style dispatch.
* ``capabilities`` — the ``--full`` flag bypasses the JSON envelope to
  emit a multi-section text dump, so the standard
  :func:`dispatch_primitive` adapter (which always emits an envelope)
  can't host it.
* ``describe`` — 3-source resolution (procedure → skill → primitive);
  the branching reads from package data, not from a single primitive
  call.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from hpc_agent.cli._helpers import EXIT_OK, _emit, _err, _ok


def cmd_install_commands(args: argparse.Namespace) -> int:
    """Copy bundled slash commands + skills into ~/.claude/.

    The pip-install entry point: after ``pip install hpc-agent`` this
    wires the agent assets shipped in the wheel into Claude Code's
    user-global config dir. Idempotent (overwrites in place). Use
    ``--dry-run`` to preview without writing.
    """
    from hpc_agent.agent_assets import install_agent_assets

    claude_dir = Path(args.claude_dir).expanduser() if args.claude_dir else None
    summary = install_agent_assets(claude_dir=claude_dir, dry_run=args.dry_run)
    _emit({"ok": True, "idempotent": True, "data": summary})
    return EXIT_OK


def cmd_setup(args: argparse.Namespace) -> int:
    """One-shot setup: install assets; optionally probe a cluster.

    Installs the bundled slash commands + skills into ``~/.claude/``.
    With ``--cluster <name>``, also probes the cluster environment
    (SSH agent, ssh + file-transfer transport on PATH, ``clusters.yaml``
    parses, TCP :22 reachable) and — on a green probe — writes the
    24h cache marker that ``/submit-hpc``'s Step 6b gate reads, so the
    first submit in this experiment doesn't repeat the probe.

    The marker is scoped to ``--experiment-dir`` (default: cwd)
    because the Step 6b gate reads from ``JournalLayout(experiment_dir)``
    — run setup from your experiment directory or pass ``--experiment-dir``.

    Idempotent: re-run after fixing your SSH agent to refresh the
    marker. Always returns ``EXIT_OK`` on a successful primitive call
    — callers branch on ``data.preflight.all_ok``.
    """
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


def cmd_capabilities(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at hpc_agent._kernel.extension.capabilities.

    ``--full`` bypasses the JSON envelope to emit a multi-section text
    blob (the ``llms-full`` dump). Documented exception to the
    stdout-is-JSON contract; analogous to ``--help``.
    """
    from hpc_agent._kernel.extension.capabilities import capabilities
    from hpc_agent._kernel.registry.operations import render_llms_full

    if getattr(args, "full", False):
        # Human/LLM-mode: emit a multi-section text blob (NOT the JSON
        # envelope) modeled on Modal's llms-full.txt pattern. Documented
        # exception to the stdout-is-JSON contract; analogous to --help.
        sys.stdout.write(render_llms_full())
        sys.stdout.flush()
        return EXIT_OK

    # Lazy-import to avoid a circular import: _live_subcommands() walks
    # the argparse tree built by hpc_agent.cli.parser, which transitively
    # imports this module to wire setup's verbs into the tree.
    from hpc_agent.cli.dispatch import _live_subcommands

    _ok(capabilities(subcommands=_live_subcommands()), name="capabilities")
    return EXIT_OK


def cmd_describe(args: argparse.Namespace) -> int:
    """Resolve a name to its content from package data.

    A delegated worker calls this to fetch a cross-reference it reaches
    on its branch — a worker-prompt procedure, a skill it is pointed
    at, a primitive whose contract it needs — instead of the prompt
    pre-stitching every possible reference. Resolution order:

    1. Worker-prompt procedure (``hpc_agent/worker_prompts/<name>.md``,
       with plugin overlay) — returns ``kind: "procedure"``.
    2. Inline skill (``slash_commands/skills/<name>/SKILL.md``) —
       returns ``kind: "skill"``.
    3. Primitive in the operations catalog — returns ``kind:
       "primitive"`` with its contract.
    """
    name = args.name
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


def register(sub: argparse._SubParsersAction) -> None:
    """Register the four Tier 3 verbs under the parent subparser.

    Called from :func:`hpc_agent.cli.parser.build_parser` after the
    registry walk so verb-group nesting and override semantics line up
    with the rest of the registry-driven surface.
    """
    # capabilities
    p_cap = sub.add_parser(
        "capabilities",
        help="Machine-readable feature flags: subcommands, schedulers, schema dirs.",
    )
    p_cap.add_argument(
        "--full",
        action="store_true",
        help=(
            "Emit a plain-text llms-full dump (catalog + every primitive doc + "
            "schemas + envelope + boundary contract + cli-spec). Exception to the "
            "stdout-is-JSON contract; intended for one-shot LLM context loading."
        ),
    )
    p_cap.set_defaults(func=cmd_capabilities)

    # install-commands
    p_install = sub.add_parser(
        "install-commands",
        help=(
            "Copy the bundled slash commands and skills into "
            "~/.claude/commands/ and ~/.claude/skills/. The pip-install "
            "entry point — run once after `pip install hpc-agent` to wire "
            "the agent assets into Claude Code. Idempotent (overwrites in "
            "place). Pass --claude-dir to target a non-default config dir."
        ),
    )
    p_install.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview which commands/skills would be copied without writing.",
    )
    p_install.add_argument(
        "--claude-dir",
        type=str,
        default=None,
        help="Override the target Claude config dir. Defaults to ~/.claude.",
    )
    p_install.set_defaults(func=cmd_install_commands)

    # setup
    p_setup = sub.add_parser(
        "setup",
        help=(
            "One-shot setup: copy the bundled slash commands and skills "
            "into ~/.claude/. Run this once after `pip install "
            "hpc-agent`. Idempotent — safe to re-run."
        ),
    )
    p_setup.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview without writing.",
    )
    p_setup.add_argument(
        "--claude-dir",
        type=str,
        default=None,
        help="Override the target Claude config dir. Defaults to ~/.claude.",
    )
    p_setup.add_argument(
        "--cluster",
        type=str,
        default=None,
        help=(
            "Optional cluster name. When supplied, probe the cluster's "
            "environment (SSH agent, ssh/transport on PATH, clusters.yaml, "
            "TCP :22) after install and write the 24h cache marker that "
            "/submit-hpc's Step 6b gate reads."
        ),
    )
    p_setup.add_argument(
        "--experiment-dir",
        type=str,
        default=None,
        help=(
            "Experiment directory whose journal receives the preflight "
            "cache marker. Defaults to cwd. Only used when --cluster is set."
        ),
    )
    p_setup.set_defaults(func=cmd_setup)

    # describe
    p_describe = sub.add_parser(
        "describe",
        help=(
            "Print a skill's procedure or a primitive's contract from the "
            "installed package data. A delegated worker uses this to fetch "
            "a cross-reference on the branch it is executing."
        ),
    )
    p_describe.add_argument(
        "name",
        help=("A skill name (e.g. hpc-submit) or a primitive name (e.g. submit-flow)."),
    )
    p_describe.set_defaults(func=cmd_describe)


__all__ = [
    "cmd_capabilities",
    "cmd_describe",
    "cmd_install_commands",
    "cmd_setup",
    "register",
]
